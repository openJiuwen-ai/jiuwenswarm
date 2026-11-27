# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""jiuwenclaw.gateway.slash_command 单元测试."""

import importlib.util
from pathlib import Path
import sys
import pytest

# 避免 `import jiuwenclaw.gateway.slash_command` 触发 `jiuwenclaw.gateway.__init__`
# 进而级联导入 channel/wecom/lark_oapi，在开启 warning->error 的 CI 中导致 collection 失败。
_MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "jiuwenclaw" / "gateway" / "slash_command.py"
)
_SPEC = importlib.util.spec_from_file_location("ut_gateway_slash_command", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)

CONTROL_MESSAGE_TEXTS = _MOD.CONTROL_MESSAGE_TEXTS
FIRST_BATCH_REGISTRY = _MOD.FIRST_BATCH_REGISTRY
ParsedControlAction = _MOD.ParsedControlAction
VALID_MODE_LINES = _MOD.VALID_MODE_LINES
VALID_SWITCH_LINES = _MOD.VALID_SWITCH_LINES
format_skills_list_for_notice = _MOD.format_skills_list_for_notice
is_control_like_for_im_batching = _MOD.is_control_like_for_im_batching
parse_channel_control_text = _MOD.parse_channel_control_text


@pytest.mark.parametrize(
    ("text", "action", "mode_sub", "switch_sub"),
    [
        ("", ParsedControlAction.NONE, None, None),
        ("hello", ParsedControlAction.NONE, None, None),
        ("/new_session", ParsedControlAction.NEW_SESSION_OK, None, None),
        ("/new_session x", ParsedControlAction.NEW_SESSION_BAD, None, None),
        ("/mode agent", ParsedControlAction.MODE_OK, "agent", None),
        ("/mode code", ParsedControlAction.MODE_OK, "code", None),
        ("/mode team", ParsedControlAction.MODE_OK, "team", None),
        ("/mode agent.plan", ParsedControlAction.MODE_OK, "agent.plan", None),
        ("/mode agent.fast", ParsedControlAction.MODE_OK, "agent.fast", None),
        ("/mode code.plan", ParsedControlAction.MODE_OK, "code.plan", None),
        ("/mode code.normal", ParsedControlAction.MODE_OK, "code.normal", None),
        ("/mode plan", ParsedControlAction.MODE_BAD, None, None),
        ("/mode", ParsedControlAction.MODE_BAD, None, None),
        ("/switch plan", ParsedControlAction.SWITCH_OK, None, "plan"),
        ("/switch fast", ParsedControlAction.SWITCH_OK, None, "fast"),
        ("/switch normal", ParsedControlAction.SWITCH_OK, None, "normal"),
        ("/switch code", ParsedControlAction.SWITCH_BAD, None, None),
        ("/switch", ParsedControlAction.SWITCH_BAD, None, None),
        ("/skills", ParsedControlAction.NONE, None, None),
        ("/skills list", ParsedControlAction.SKILLS_OK, None, None),
        ("/skills   list", ParsedControlAction.SKILLS_OK, None, None),
        ("/skills extra", ParsedControlAction.NONE, None, None),
        ("line1\nline2", ParsedControlAction.NONE, None, None),
    ],
)
def test_parse_channel_control_text(
    text: str,
    action: ParsedControlAction,
    mode_sub: str | None,
    switch_sub: str | None,
) -> None:
    p = parse_channel_control_text(text)
    assert p.action is action
    assert p.mode_subcommand == mode_sub
    assert p.switch_subcommand == switch_sub


def test_control_message_texts_contains_mode_variants_and_skills() -> None:
    assert "/new_session" in CONTROL_MESSAGE_TEXTS
    assert "/skills list" in CONTROL_MESSAGE_TEXTS
    assert VALID_MODE_LINES <= CONTROL_MESSAGE_TEXTS
    assert VALID_SWITCH_LINES <= CONTROL_MESSAGE_TEXTS
    assert "/mode team" in CONTROL_MESSAGE_TEXTS
    assert "/mode code" in CONTROL_MESSAGE_TEXTS
    assert "/mode agent.plan" in CONTROL_MESSAGE_TEXTS
    assert "/mode code.normal" in CONTROL_MESSAGE_TEXTS
    assert "/switch normal" in CONTROL_MESSAGE_TEXTS


def test_is_control_like_for_im_batching() -> None:
    assert is_control_like_for_im_batching("/new_session")
    assert is_control_like_for_im_batching("/mode agent")
    assert is_control_like_for_im_batching("/mode agent.plan")
    assert is_control_like_for_im_batching("/mode foo")
    assert is_control_like_for_im_batching("/switch plan")
    assert is_control_like_for_im_batching("/switch foo")
    assert is_control_like_for_im_batching("/new_sessionoops")
    assert is_control_like_for_im_batching("/skills list")
    assert is_control_like_for_im_batching("/skills   list")
    assert not is_control_like_for_im_batching("/skills")
    assert not is_control_like_for_im_batching("/skills extra")
    assert not is_control_like_for_im_batching("")
    assert not is_control_like_for_im_batching("a\nb")


def test_format_skills_list_for_notice() -> None:
    out = format_skills_list_for_notice(
        {
            "skills": [
                {"name": "a", "description": "d1", "source": "local"},
                {"name": "b"},
            ]
        }
    )
    assert "【技能列表】" in out
    assert "a" in out
    assert "b" in out


def test_first_batch_registry_ids() -> None:
    ids = {e.id for e in FIRST_BATCH_REGISTRY}
    assert ids == {"new_session", "mode", "switch", "skills", "resume", "workspace_dir"}
