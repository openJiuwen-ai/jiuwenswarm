"""Real Web driver and Core persist changed group attachments across invocations."""

import json
from datetime import timedelta

import pytest

from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.group_directory import SECTION
from jiuwenswarm.common.schema.message import ReqMethod
from tests.integration_tests.jiuwenmate.test_web_real_core import NOW, Server, model


def directory_messages(messages):
    return [message for message in messages if SECTION in (getattr(message, "metadata", {}) or {}).get("state", {})]


@pytest.mark.asyncio
async def test_directory_reaches_real_model_and_survives_rebuild_restart_and_departure(tmp_path, model):
    server = Server(tmp_path)
    agent = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]["agent_id"]
    groups = GroupChatRepository(server.db)
    for platform, chat, name in (("feishu", "oc_directory", "飞书筹备群"), ("dingtalk", "cid_directory", "钉钉执行群")):
        group = groups.resolve("local", platform, chat, now=NOW)
        groups.set_name("local", group.group_chat_id, name)
        groups.observe_access("local", agent, group.group_chat_id, "app-" + platform,
                              observed_at=NOW, message_time=NOW, source_ref=f"{platform}:message:first")
    try:
        assert (await server.ask(agent, "which-groups"))["outcome"] == "completed"
        assert (await server.ask(agent, "same-directory"))["outcome"] == "completed"
        assert len(directory_messages(model.calls[0])) == 1
        assert len(directory_messages(model.calls[1])) == 1
        first = directory_messages(model.calls[0])[0].content
        assert "feishu" in first and "dingtalk" in first and '"active_count":2' in first
        assert len(model.calls) == 2
    finally:
        await server.adapter.aclose()
    group = groups.resolve("local", "feishu", "oc_directory", now=NOW)
    groups.set_name("local", group.group_chat_id, "飞书改名后")
    restarted = Server(tmp_path)
    try:
        assert (await restarted.ask(agent, "renamed-directory"))["outcome"] == "completed"
        changed = directory_messages(model.calls[-1])
        assert len(changed) == 2
        assert "群名更改" in changed[-1].content and "飞书改名后" in changed[-1].content
        groups.update_membership("local", agent, group.group_chat_id, "app-feishu", status="left",
                                 observed_at=NOW + timedelta(seconds=30), source_ref="event:left")
        assert (await restarted.ask(agent, "left-directory"))["outcome"] == "completed"
        removal = directory_messages(model.calls[-1])
        assert len(removal) == 3
        content = removal[-1].content.split("<workswarm_group_directory>\n", 1)[1]
        payload = json.loads(content.split("\n</workswarm_group_directory>", 1)[0])
        assert payload["directory"]["active_count"] == 1
        assert len(payload["directory"]["groups"]) == 1
        assert payload["directory"]["groups"][0]["platform"] == "dingtalk"
        assert payload["changes"] == [{"event": "已移出", "group_id": group.group_chat_id, "name": "飞书改名后"}]
        assert (await restarted.ask(agent, "same-after-removal"))["outcome"] == "completed"
        assert len(directory_messages(model.calls[-1])) == 3
        assert len(model.calls) == 5
    finally:
        await restarted.adapter.aclose()
