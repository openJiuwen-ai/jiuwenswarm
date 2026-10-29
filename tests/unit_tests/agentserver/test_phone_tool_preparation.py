# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Handset tools stay optional without changing trusted tool identities."""

import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


_REGISTRATION_NAMES = [
    "get_user_location", "create_note", "search_notes", "modify_note",
    "create_calendar_event", "search_calendar_event", "search_contact",
    "search_photo_gallery", "upload_photo", "search_file", "upload_file",
    "call_phone", "send_message", "search_message", "create_alarm",
    "search_alarms", "modify_alarm", "delete_alarm", "query_collection",
    "add_collection", "delete_collection", "save_media_to_gallery",
    "save_file_to_file_manager", "convert_timestamp_to_utc8_time",
    "view_push_result", "image_reading", "xiaoyi_gui_agent",
]


@pytest.mark.parametrize("statement", [
    "from jiuwenswarm.server.runtime.agent_adapter import phone_tool_factory",
    (
        "from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission "
        "import readonly_tool_bindings; "
        "from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.timestamp_tool "
        "import convert_timestamp_to_utc8_time; "
        "assert readonly_tool_bindings.convert_timestamp_to_utc8_time "
        "is convert_timestamp_to_utc8_time"
    ),
    "from jiuwenswarm.server.runtime.agent_adapter import interface_deep",
])
def test_base_execution_imports_do_not_prepare_handset_tools(statement, tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = f"""
import importlib.abc, sys
sys.path.insert(0, {str(root)!r})
prefix = 'jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.'
allowed = {{prefix + 'timestamp_tool', prefix + 'utils'}}
attempted = []
class RejectHandsetTools(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(prefix) and fullname not in allowed:
            attempted.append(fullname)
            raise AssertionError('handset tool imported by base execution: ' + fullname)
sys.meta_path.insert(0, RejectHandsetTools())
{statement}
assert not attempted, attempted
assert all(not name.startswith(prefix) or name in allowed for name in sys.modules)
print('HANDSET_IMPORT_BOUNDARY_OK')
"""
    env = dict(
        os.environ,
        JIUWENSWARM_DATA_DIR=str(tmp_path / "data"),
        JIUWENSWARM_HOME=str(tmp_path / "home"),
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "HANDSET_IMPORT_BOUNDARY_OK" in result.stdout


def test_phone_factory_preserves_exports_order_and_original_tool_objects():
    from jiuwenswarm.agents.harness.common.tools import xiaoyi_phone_tools
    from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.timestamp_tool import (
        convert_timestamp_to_utc8_time,
    )
    from jiuwenswarm.server.runtime.agent_adapter.phone_tool_factory import (
        get_xiaoyi_phone_tools,
    )

    expected_exports = _REGISTRATION_NAMES[:23] + [
        "view_push_result", "convert_timestamp_to_utc8_time",
        "xiaoyi_gui_agent", "image_reading",
    ]
    assert xiaoyi_phone_tools.__all__ == expected_exports
    tools = get_xiaoyi_phone_tools()
    assert [tool.card.name for tool in tools] == _REGISTRATION_NAMES
    assert all(tool is getattr(xiaoyi_phone_tools, name)
               for tool, name in zip(tools, _REGISTRATION_NAMES))
    assert all(first is second for first, second in zip(tools, get_xiaoyi_phone_tools()))
    assert tools[23] is convert_timestamp_to_utc8_time
    # Direct submodule imports remain compatible with existing callers.
    timestamp_module = importlib.import_module(xiaoyi_phone_tools.__name__ + ".timestamp_tool")
    assert timestamp_module.convert_timestamp_to_utc8_time is tools[23]
    with pytest.raises(AttributeError, match="unknown_handset_tool"):
        getattr(xiaoyi_phone_tools, "unknown_handset_tool")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, registered, expected_count", [
    (False, False, 0),
    (True, False, 27),
    (True, True, 0),
])
async def test_phone_capability_gates_tool_registration(
    monkeypatch, enabled, registered, expected_count,
):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep, phone_tool_factory

    config = {"channels": {"xiaoyi": {"phone_tools_enabled": enabled}}}
    captured_tools = []
    factory_calls = []
    factory = phone_tool_factory.get_xiaoyi_phone_tools

    def get_phone_tools():
        factory_calls.append(True)
        return factory()

    monkeypatch.setattr(phone_tool_factory, "get_xiaoyi_phone_tools", get_phone_tools)
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(interface_deep, "is_paid_search_enabled", lambda: False)
    monkeypatch.setattr(interface_deep, "SkillToolkit", lambda **kwargs: SimpleNamespace(get_tools=lambda: []))
    monkeypatch.setattr(interface_deep, "SymphonyToolkit", lambda: SimpleNamespace(get_tools=lambda _: []))
    adapter = SimpleNamespace(
        _instance=object(),
        _vision_model_config=None,
        _video_model_config=False,
        _image_gen_model_config=False,
        _video_gen_model_config=False,
        _visual_gen_model_config=False,
        _xiaoyi_phone_tools_registered=registered,
        _skill_manager=object(),
        _config_base_cache=config,
        _register_shared_tool=captured_tools.append,
        _register_agent_owned_tool=lambda tool, owner: None,
        _iter_runtime_audio_tools=lambda owner: [],
        _register_multimodal_tools=lambda owner: [],
        _set_multimodal_runtime_state=lambda tools: None,
        _skill_retrieval_tools_enabled_for_runtime=lambda _: False,
        _tool_retrieval_tools_enabled_for_runtime=lambda _: False,
    )

    cards = await interface_deep.JiuWenSwarmDeepAdapter._get_tool_cards(adapter, "phone-test")
    phone_tools = [tool for tool in captured_tools if tool.card.name in _REGISTRATION_NAMES]
    assert len(phone_tools) == expected_count
    assert len(factory_calls) == bool(expected_count)
    assert adapter._xiaoyi_phone_tools_registered is (registered or bool(expected_count))
    if expected_count:
        assert [tool.card.name for tool in phone_tools] == _REGISTRATION_NAMES
        assert all(actual is original for actual, original in zip(phone_tools, factory()))
        assert all(any(card is tool.card for card in cards) for tool in phone_tools)
