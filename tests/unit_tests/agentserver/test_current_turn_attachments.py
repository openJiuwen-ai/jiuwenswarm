"""Regression coverage for request attachments and their model-message binding."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    ensure_multimodal_image_window_mutator,
    extract_current_turn_attachments,
    extract_multimodal_image_files,
    prepare_multimodal_image_messages,
    render_current_turn_attachments,
    reset_current_multimodal_image_files,
    set_current_multimodal_image_files,
)


@pytest.fixture
def image_file(tmp_path):
    path = tmp_path / "current.png"
    path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+jq/QAAAAASUVORK5CYII="
    ))
    return {"path": str(path), "filename": "current.png", "mime_type": "image/png"}


def test_desktop_attachments_survive_a_leading_plan_reminder():
    query = (
        "<system-reminder>Plan mode is active.</system-reminder>\n\n"
        '/skill reader <claw_context>{"files":[{"name":"B.jpg",'
        '"path":"/uploads/B.jpg"}]}</claw_context>\n\n识别附件'
    )

    assert extract_current_turn_attachments({"query": query}) == [
        {"filename": "B.jpg", "path": "/uploads/B.jpg", "mime_type": "image/jpeg"}
    ]


def test_native_image_stays_on_user_query_with_a_leading_reminder(image_file):
    query = "<system-reminder>Plan mode is active.</system-reminder>\n\n识别本轮附件"
    messages = [
        {"role": "user", "content": "之前生成的图片A"},
        {"role": "user", "content": query},
        {"role": "user", "content": "<system-reminder>Runtime settings.</system-reminder>"},
    ]

    updated, count = prepare_multimodal_image_messages(messages, [image_file])

    assert count == 1
    assert updated[0] == messages[0]
    assert isinstance(updated[1]["content"], list)
    payload = json.loads(updated[1]["content"][0]["text"])
    assert payload["query"] == query
    assert payload["file"][0]["path"] == image_file["path"]
    assert updated[2] == messages[2]


@pytest.mark.parametrize("params", [
    {"files": [{"path": "/uploads/B.jpg", "type": "image/jpeg"}]},
    {"files": {"uploaded_images": [{"path": "/uploads/B.jpg", "mimeType": "image/jpeg"}]}},
    {"media_items": [{"type": "image", "filename": "B.jpg", "path": "/uploads/B.jpg"}]},
    {"attachments": [{"name": "B.jpg", "path": "/uploads/B.jpg"}]},
    {"content": '<claw_context>{"files":[{"name":"B.jpg","path":"/uploads/B.jpg"}]}</claw_context>\n\n识图'},
    {"query": '/skill reader /skill editor <claw_context>{"files":[{"name":"B.jpg","path":"/uploads/B.jpg"}]}</claw_context>\n\n识图'},
])
def test_attachment_carriers_share_the_same_file_record(params):
    assert extract_current_turn_attachments(params) == [
        {"filename": "B.jpg", "path": "/uploads/B.jpg", "mime_type": "image/jpeg"}
    ]
    assert extract_multimodal_image_files(params) == [
        {"type": "image", "filename": "B.jpg", "path": "/uploads/B.jpg", "mime_type": "image/jpeg"}
    ]


def test_document_keeps_original_and_parsed_paths_as_one_attachment():
    document = {
        "filename": "报告.docx",
        "path": "/uploads/report.txt",
        "original_path": "/uploads/report.docx",
        "text_path": "/uploads/report.txt",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    params = {"media_items": [document], "files": {"uploaded_documents": [document]}}

    assert extract_current_turn_attachments(params) == [{
        "filename": "报告.docx",
        "path": "/uploads/report.docx",
        "text_path": "/uploads/report.txt",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }]
    assert extract_multimodal_image_files(params) == []


@pytest.mark.parametrize("text_truncated", [True, False])
def test_document_parse_metadata_survives_deduplication(text_truncated):
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    media_item = {
        "filename": "周报.docx", "path": "/uploads/report.txt",
        "original_path": "/uploads/report.docx", "text_path": "/uploads/report.txt",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    file_record = {**media_item, "parser": "WordParser", "text_truncated": text_truncated}
    files = {"uploaded_documents": [file_record]}
    built = build_user_prompt(
        "总结本轮文档", files=files, media_items=[media_item], channel="web", language="zh",
    )
    attachments = _attachment_payload(built.model_query)

    assert len(attachments) == 1
    assert attachments[0]["parser"] == "WordParser"
    assert attachments[0]["text_truncated"] is text_truncated
    assert attachments[0]["path"] == "/uploads/report.docx"
    assert attachments[0]["text_path"] == "/uploads/report.txt"
    assert "files_updated_by_user" not in built.context
    assert "parser" not in media_item
    assert files["uploaded_documents"][0] == file_record


def test_windows_duplicates_merge_metadata_without_changing_file_order():
    params = {
        "media_items": [{"path": r"C:\Uploads\B.jpg"}],
        "files": [
            {"path": "c:/uploads/b.jpg", "name": "用户图片.jpg", "size_bytes": 123},
            {"path": r"C:\Other\B.jpg"},
        ],
    }
    assert extract_current_turn_attachments(params) == [
        {"filename": "用户图片.jpg", "path": r"C:\Uploads\B.jpg", "mime_type": "image/jpeg", "size_bytes": 123},
        {"filename": "B.jpg", "path": r"C:\Other\B.jpg", "mime_type": "image/jpeg"},
    ]


def test_posix_paths_keep_case_and_input_order():
    params = {"files": [{"path": "/uploads/b.jpg"}, {"path": "/uploads/B.jpg"}]}
    assert [item["path"] for item in extract_current_turn_attachments(params)] == [
        "/uploads/b.jpg", "/uploads/B.jpg",
    ]


@pytest.mark.parametrize("params", [
    None, [], {}, {"files": []},
    {"files": [None, 123, {}, {"path": None}, {"path": {"invalid": True}}]},
    {"query": "<claw_context>not-json</claw_context>\n\nhello"},
    {"query": '<claw_context>{"files":"not-a-list"}</claw_context>\n\nhello'},
])
def test_empty_or_malformed_attachments_do_not_create_context(params):
    assert extract_current_turn_attachments(params) == []
    assert render_current_turn_attachments(extract_current_turn_attachments(params)) == ""


@pytest.mark.parametrize("reminder", [
    "<system-reminder>Runtime.</system-reminder>",
    "<system-reminder>Runtime.</system-reminder>\n<system-reminder>Mode.</system-reminder>",
    [{"type": "text", "text": "<system-reminder>Runtime.</system-reminder>"}],
])
def test_native_image_skips_pure_reminders_and_preserves_input_messages(image_file, reminder):
    messages = [
        {"role": "user", "content": "识别这张图"},
        {"role": "user", "content": reminder},
    ]
    updated, count = prepare_multimodal_image_messages(messages, [image_file])
    assert count == 1
    assert messages[0]["content"] == "识别这张图"
    assert updated[1] == messages[1]
    assert json.loads(updated[0]["content"][0]["text"])["query"] == "识别这张图"
    assert updated[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_native_images_support_object_messages(image_file):
    message = SimpleNamespace(role="user", content="识别附件")
    updated, count = prepare_multimodal_image_messages([message], [image_file])
    assert count == 1
    assert message.content == "识别附件"
    assert json.loads(updated[0].content[0]["text"])["file"][0]["path"] == image_file["path"]


def test_explicit_empty_image_list_does_not_reuse_context_images(image_file):
    messages = [{"role": "user", "content": "本轮没有图片"}]
    token = set_current_multimodal_image_files([image_file])
    try:
        updated, count = prepare_multimodal_image_messages(messages, [])
        assert count == 0
        assert updated == messages
    finally:
        reset_current_multimodal_image_files(token)


@pytest.mark.asyncio
async def test_next_turn_without_images_removes_previous_window_mutator(image_file):
    context = SimpleNamespace(_window_mutators=[])
    assert ensure_multimodal_image_window_mutator(context, [image_file])
    window = SimpleNamespace(context_messages=[{"role": "user", "content": "识图"}])
    updated = await context._window_mutators[0](context, window)
    assert isinstance(updated.context_messages[0]["content"], list)
    assert not ensure_multimodal_image_window_mutator(context, [])
    assert context._window_mutators == []


def _attachment_payload(query):
    return json.loads(query.split("<attachments>\n", 1)[1].split("\n</attachments>", 1)[0])


@pytest.mark.parametrize("channel, params", [
    ("xiaoyi", {"query": "处理这个附件", "files": [{"path": "/uploads/B.jpg", "type": "image/jpeg"}]}),
    ("web", {"query": '<claw_context>{"files":[{"name":"B.jpg","path":"/uploads/B.jpg"}]}</claw_context>\n\n处理这个附件'}),
    ("web", {"content": "处理这个附件", "media_items": [{"path": "/uploads/B.jpg", "type": "image"}]}),
])
def test_build_inputs_binds_files_to_query_without_changing_request(monkeypatch, channel, params):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter import interface

    monkeypatch.setattr(interface, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface, "get_memory_mode", lambda _config: "disabled")
    request = AgentRequest(request_id="attachment-unit", channel_id=channel, params=params.copy())
    inputs, _memory_mode, raw_query = interface.JiuWenSwarm()._build_inputs(request)

    expected_query = params.get("query") or params.get("content")
    assert raw_query == expected_query
    assert request.params == params
    assert inputs["query"].startswith("处理这个附件")
    assert inputs["query"].count("/uploads/B.jpg") == 1
    assert _attachment_payload(inputs["query"]) == [
        {"filename": "B.jpg", "path": "/uploads/B.jpg", "mime_type": "image/jpeg"}
    ]


def test_prompt_without_attachments_preserves_existing_context_and_directives():
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    built = build_user_prompt(
        "hello", files={}, channel="web", language="zh",
        metadata={"interaction_context": "connector context"},
    )
    assert built.user_query == "hello"
    assert built.attachment_context == ""
    assert built.model_query == "\nconnector context\n\nhello"


@pytest.mark.parametrize("channel", ["desktop", "xiaoyi"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("suffixes", ["workspace", "workspace_cron", "cron_workspace"])
def test_prompt_places_attachments_between_user_text_and_channel_suffix(channel, newline, suffixes):
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    user_text = "把这些附件复制到桌面。\n保留原文件名。".replace("\n", newline)
    workspace = '\n\n<claw_workspace>{"path":"D:/ws/proj"}</claw_workspace>\n【工作空间】当前项目目录是 `D:/ws/proj`。'
    cron = "\n\n<claw_cron_create></claw_cron_create>\n【定时任务】创建任务时使用当前对话。"
    suffix = {
        "workspace": workspace, "workspace_cron": workspace + cron, "cron_workspace": cron + workspace,
    }[suffixes].replace("\n", newline)
    query = user_text + suffix
    files = [{"path": "/uploads/B.jpg"}, {"path": "/uploads/C.docx"}]
    if channel == "desktop":
        query = "<claw_context>" + json.dumps({"files": files}) + "</claw_context>\n\n" + query
        files = {}
    built = build_user_prompt(query, files=files, channel=channel, language="zh")

    assert built.model_query.startswith(user_text + "\n\n")
    assert built.model_query.endswith(suffix)
    assert built.model_query.index("</attachments>") < built.model_query.index("<claw_")
    assert [item["path"] for item in _attachment_payload(built.model_query)] == [
        "/uploads/B.jpg", "/uploads/C.docx",
    ]
    assert built.user_query == user_text + suffix


@pytest.mark.parametrize("query", [
    "解释【工作空间】当前项目目录是这句话。",
    "处理附件\n\n<claw_workspace>not-json</claw_workspace>\n【工作空间】示例",
    '处理附件\n\n<claw_workspace>{"path":""}</claw_workspace>\n【工作空间】示例',
    '处理附件\n\n<claw_workspace>{"path":"D:/ws/proj"}</claw_workspace>\n【工作空间】示例\n还有用户正文。',
])
def test_attachment_ordering_preserves_literal_or_invalid_protocol_text(query):
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    built = build_user_prompt(query, files=[{"path": "/uploads/B.jpg"}], channel="xiaoyi", language="zh")

    assert built.model_query.startswith(query + "\n\n")
    assert built.user_query == query
    assert built.model_query.count("/uploads/B.jpg") == 1


def test_channel_suffix_without_attachments_is_unchanged():
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    query = '处理上一轮生成的图片\n\n<claw_workspace>{"path":"D:/ws/proj"}</claw_workspace>\n【工作空间】当前项目目录是 `D:/ws/proj`。'
    built = build_user_prompt(query, files={}, channel="xiaoyi", language="zh")

    assert built.model_query == query
    assert built.attachment_context == ""


@pytest.mark.parametrize("files", [
    {"uploaded_images": [{"path": "/uploads/B.jpg"}, {"path": "/uploads/C.jpg"}]},
    [{"path": "/uploads/B.jpg"}, {"path": "/uploads/C.jpg"}],
])
def test_attachment_metadata_is_only_in_the_current_user_message(files):
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    built = build_user_prompt("比较这些附件", files=files, channel="web", language="zh")

    assert "files_updated_by_user" not in built.context
    assert built.context["source"] == "web"
    assert built.user_query == "比较这些附件"
    assert built.model_query.count("/uploads/B.jpg") == 1
    assert built.model_query.count("/uploads/C.jpg") == 1


@pytest.mark.parametrize("extra_context", [{}, {"category": "文档生成", "skillNames": ["reader"]}])
def test_desktop_prompt_moves_files_but_preserves_other_context(extra_context):
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    context = {"files": [{"name": "B.jpg", "path": "/uploads/B.jpg"}], **extra_context}
    prefix = "<system-reminder>Plan mode is active.</system-reminder>\n\n/skill reader "
    query = prefix + "<claw_context>" + json.dumps(context) + "</claw_context>\n\n处理附件"
    built = build_user_prompt(query, files={}, channel="desktop", language="zh")

    assert built.user_query.startswith(prefix)
    assert built.user_query.endswith("处理附件")
    assert built.model_query.count("/uploads/B.jpg") == 1
    assert "files_updated_by_user" not in built.context
    if extra_context:
        remaining = json.loads(built.user_query.split("<claw_context>", 1)[1].split("</claw_context>", 1)[0])
        assert remaining == extra_context
    else:
        assert "<claw_context>" not in built.user_query


def test_unrecognized_file_metadata_remains_in_legacy_system_context():
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    built = build_user_prompt("处理附件", files={"test.py": "content"}, channel="web", language="zh")

    assert json.loads(built.context["files_updated_by_user"]) == {"test.py": "content"}
    assert built.attachment_context == ""


def test_deduplication_preserves_other_legacy_file_fields():
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    built = build_user_prompt("处理附件", files={
        "uploaded_images": [{"path": "/uploads/B.jpg"}], "test.py": "content",
    }, channel="web", language="zh")

    assert json.loads(built.context["files_updated_by_user"]) == {"test.py": "content"}
    assert built.model_query.count("/uploads/B.jpg") == 1


@pytest.mark.parametrize("carrier", ["web", "desktop"])
def test_deduplication_keeps_file_records_not_in_the_attachment_block(carrier):
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    records = [{"path": "/uploads/B.jpg"}, {"url": "https://example.com/C.jpg"}]
    query = "处理附件"
    files = {"uploaded_images": records}
    if carrier == "desktop":
        query = "<claw_context>" + json.dumps({"files": records}) + "</claw_context>\n\n处理附件"
        files = {}
    built = build_user_prompt(query, files=files, channel=carrier, language="zh")

    assert built.model_query.count("/uploads/B.jpg") == 1
    if carrier == "desktop":
        remaining = json.loads(built.user_query.split("<claw_context>", 1)[1].split("</claw_context>", 1)[0])
        assert remaining == {"files": [{"url": "https://example.com/C.jpg"}]}
    else:
        assert json.loads(built.context["files_updated_by_user"]) == {
            "uploaded_images": [{"url": "https://example.com/C.jpg"}],
        }


@pytest.mark.parametrize("query", [
    "处理正文中的 <claw_context>{\"files\":[]}</claw_context> 示例",
    "<claw_context>not-json</claw_context>\n\n处理附件",
])
def test_prompt_deduplication_preserves_literal_or_malformed_context(query):
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    built = build_user_prompt(query, files=[{"path": "/uploads/B.jpg"}], channel="desktop", language="zh")

    assert built.user_query == query
    assert built.model_query.count("/uploads/B.jpg") == 1


@pytest.mark.parametrize("prefix, key", [
    ("", "query"),
    ("/skill reader /skill editor ", "query"),
    ("<system-reminder>Plan mode is active.</system-reminder>\n\n/skill reader ", "query"),
    ("", "content"),
])
def test_image_tool_question_uses_desktop_user_body(prefix, key):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    query = prefix + '<claw_context>{"files":[{"name":"B.jpg","path":"/uploads/B.jpg"}]}</claw_context>\n\n识别这张图片'
    request = AgentRequest(request_id="image-question-unit", params={key: query})
    updated = JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
        request, {"query": query}, enable_read_image_multimodal=False,
    )
    context = json.loads(updated["query"].rsplit("\n", 1)[1])

    assert context["question"] == "识别这张图片"
    assert context["mediaItems"][0]["mediaPath"] == "/uploads/B.jpg"
    assert request.params[key] == query


@pytest.mark.parametrize("profile", ["default", "full_access"])
@pytest.mark.parametrize("desktop_prefix", ["", '<claw_context>{"category":"识图"}</claw_context>\n\n'])
def test_image_tool_question_excludes_mobile_workspace_suffix(profile, desktop_prefix):
    from jiuwenswarm.common.permission_profile import with_workspace_directive
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    query = with_workspace_directive(desktop_prefix + "这是什么", "D:/ws/proj", profile)
    request = AgentRequest(request_id="image-workspace-unit", params={
        "query": query, "files": [{"path": "/uploads/B.jpg"}],
    })
    updated = JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
        request, {"query": query}, enable_read_image_multimodal=False,
    )

    assert json.loads(updated["query"].rsplit("\n", 1)[1])["question"] == "这是什么"
    assert updated["query"].startswith(query)
    assert request.params["query"] == query


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("suffixes", ["cron", "workspace_cron", "cron_workspace"])
def test_image_tool_question_excludes_combined_protocol_suffixes(newline, suffixes):
    from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import extract_image_tool_question

    cron = "\n\n<claw_cron_create></claw_cron_create>\n【定时任务】创建任务时使用当前对话。"
    workspace = '\n\n<claw_workspace>{"path":"D:/ws/proj"}</claw_workspace>\n【工作空间】当前项目目录是 `D:/ws/proj`。'
    suffix = {"cron": cron, "workspace_cron": workspace + cron, "cron_workspace": cron + workspace}[suffixes]
    query = ("这是什么\n请描述颜色。" + suffix).replace("\n", newline)

    assert extract_image_tool_question(query) == "这是什么" + newline + "请描述颜色。"


@pytest.mark.parametrize("suffixes", ["time", "time_cron"])
def test_image_tool_question_excludes_time_directive_suffix(suffixes):
    """未选工作空间时的 <claw_time> 尾段（含与 cron 组合）不进识图提问。"""
    from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import extract_image_tool_question

    time_suffix = (
        '\n\n<claw_time>{"timestamp":"2026-10-10 17:20:00"}</claw_time>\n'
        "【当前时间】现在是 2026-10-10 17:20:00，回答依赖当前时间的问题时以此为准。"
    )
    cron = "\n\n<claw_cron_create></claw_cron_create>\n【定时任务】创建任务时使用当前对话。"
    suffix = {"time": time_suffix, "time_cron": time_suffix + cron}[suffixes]

    assert extract_image_tool_question("这是什么" + suffix) == "这是什么"


@pytest.mark.parametrize("query", [
    "解释【工作空间】当前项目目录是这句话。",
    "这是什么\n\n<claw_workspace>not-json</claw_workspace>\n【工作空间】示例",
    '这是什么\n\n<claw_workspace>{"path":"D:/ws/proj"}</claw_workspace>\n【工作空间】示例\n后面仍有用户正文。',
    "解释 <claw_cron_create></claw_cron_create> 标签的用途。",
])
def test_image_tool_question_preserves_unmarked_or_nonterminal_user_text(query):
    from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import extract_image_tool_question

    assert extract_image_tool_question(query) == query


@pytest.mark.parametrize("query", [
    "识别这张图片",
    "描述图片中的 <claw_context> 示例",
    "<claw_context>not-json</claw_context>\n\n识别图片",
])
def test_image_tool_question_does_not_strip_literal_or_malformed_context(query):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    request = AgentRequest(request_id="image-literal-unit", params={
        "query": query, "files": [{"path": "/uploads/B.jpg"}],
    })
    updated = JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
        request, {"query": query}, enable_read_image_multimodal=False,
    )

    assert json.loads(updated["query"].rsplit("\n", 1)[1])["question"] == query


@pytest.mark.parametrize("paths", [["/uploads/B.jpg"], ["/uploads/B.jpg", "/uploads/C.jpg"]])
def test_image_tool_context_has_default_path_only_for_single_image(paths):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    request = AgentRequest(request_id="image-tool-unit", params={
        "query": "处理图片附件", "files": [{"path": path, "type": "image/jpeg"} for path in paths],
    })
    inputs = {"query": "处理图片附件"}
    updated = JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
        request, inputs, enable_read_image_multimodal=False,
    )
    context = json.loads(updated["query"].rsplit("\n", 1)[1])
    assert [item["mediaPath"] for item in context["mediaItems"]] == paths
    if len(paths) == 1:
        assert context["mediaPath"] == "/uploads/B.jpg"
    else:
        assert "mediaPath" not in context
    assert inputs == {"query": "处理图片附件"}


def test_native_image_mode_does_not_add_image_tool_fallback():
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    request = AgentRequest(request_id="native-image-unit", params={"files": [{"path": "/uploads/B.jpg"}]})
    inputs = {"query": "识图"}
    assert JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
        request, inputs, enable_read_image_multimodal=True,
    ) == inputs


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent", "team"])
@pytest.mark.parametrize("params", [
    {"query": "处理本轮附件", "files": [{"path": "/uploads/B.jpg"}]},
    {"query": '<claw_context>{"files":[{"name":"B.jpg","path":"/uploads/B.jpg"}]}</claw_context>\n\n处理本轮附件'},
    {
        "query": '处理本轮附件\n\n<claw_workspace>{"path":"D:/ws/proj"}</claw_workspace>\n【工作空间】当前项目目录是 `D:/ws/proj`。',
        "files": [{"path": "/uploads/B.jpg"}],
    },
])
async def test_stream_keeps_attachment_binding_and_raw_user_history(monkeypatch, mode, params):
    from jiuwenswarm.agents.harness import team
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
    from jiuwenswarm.server.runtime.agent_adapter import interface, team_helpers

    history = []

    class EchoAdapter:
        @staticmethod
        async def process_message_stream_impl(request, inputs):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": inputs["query"]},
                is_complete=False,
            )

    async def has_runtime(_manager, _session_id):
        return True

    async def finalize(content, **_kwargs):
        return content

    monkeypatch.setattr(interface.JiuWenSwarm, "_ensure_adapter", lambda *_args, **_kwargs: EchoAdapter())
    monkeypatch.setattr(interface, "get_config", lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface, "append_history_record", lambda **record: history.append(record))
    monkeypatch.setattr(interface, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(interface, "finalize_assistant_response_if_a2ui", finalize)
    monkeypatch.setattr(team, "get_team_manager", lambda _channel: None)
    monkeypatch.setattr(team_helpers, "_team_session_has_runtime", has_runtime)
    request = AgentRequest(
        request_id="attachment-stream-unit", channel_id="tui", session_id="attachment-stream-unit",
        params={**params, "mode": mode},
        metadata={"enable_memory": False}, is_stream=True,
    )

    chunks = [chunk async for chunk in interface.JiuWenSwarm().process_message_stream(request)]
    final_contents = [
        chunk.payload["content"] for chunk in chunks
        if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.final"
    ]
    assert len(final_contents) == 1
    assert final_contents[0].count("/uploads/B.jpg") == 1
    assert _attachment_payload(final_contents[0]) == [
        {"filename": "B.jpg", "path": "/uploads/B.jpg", "mime_type": "image/jpeg"}
    ]
    assert [record["content"] for record in history if record["role"] == "user"] == [params["query"]]
    assert request.params["query"] == params["query"]
    if "<claw_workspace>" in params["query"]:
        assert final_contents[0].index("</attachments>") < final_contents[0].index("<claw_workspace>")
        assert final_contents[0].endswith(params["query"][len("处理本轮附件"):])
