"""user_toolkits 单元测试。

覆盖：create_user / list_users 两工具的成功路径与错误场景、
工具卡片注册（名称、必填参数）。状态文件重定向到临时目录，不触碰真实 home。
"""

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools import user_toolkits as ut
from jiuwenswarm.server.runtime.user_registry import user_store as us


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把用户状态文件重定向到临时目录。"""
    path = tmp_path / "users.json"
    monkeypatch.setattr(us, "_state_file", lambda: path)
    return path


@pytest.mark.asyncio
async def test_create_user_tool_success(state_file: Path):
    toolkit = ut.UserToolkit()
    result = await toolkit.create_user(
        "alice", "secret-pw", "sk-1234567890", "https://api.example.com/v1", "gpt-x"
    )
    assert result["success"] is True
    # 工具返回值中敏感字段必须脱敏
    assert "secret-pw" not in json.dumps(result)
    assert "sk-1234567890" not in json.dumps(result)
    # 落盘为完整记录（供 install_third_agent 注入凭据）
    record = us.get_user("alice")
    assert record is not None
    assert record["api_key"] == "sk-1234567890"


@pytest.mark.asyncio
async def test_create_user_tool_validation(state_file: Path):
    toolkit = ut.UserToolkit()
    assert (await toolkit.create_user("", "pw"))["success"] is False
    assert (await toolkit.create_user("alice", ""))["success"] is False

    await toolkit.create_user("alice", "pw")
    dup = await toolkit.create_user("alice", "pw2")
    assert dup["success"] is False
    assert "已存在" in dup["detail"]


@pytest.mark.asyncio
async def test_list_users_tool(state_file: Path):
    toolkit = ut.UserToolkit()
    empty = await toolkit.list_users()
    assert empty["success"] is True
    assert empty["items"] == []
    assert empty["detail"]

    await toolkit.create_user("alice", "secret-pw", "sk-key")
    result = await toolkit.list_users()
    assert len(result["items"]) == 1
    assert result["items"][0]["username"] == "alice"
    assert "secret-pw" not in json.dumps(result)
    assert "sk-key" not in json.dumps(result)


def test_get_tools_cards():
    tools = ut.get_user_tools()
    by_name = {t.card.name: t for t in tools}
    assert set(by_name) == {"create_user", "list_users", "delete_user"}
    assert by_name["create_user"].card.input_params["required"] == ["username", "password"]
    assert by_name["delete_user"].card.input_params["required"] == ["username"]
    props = by_name["create_user"].card.input_params["properties"]
    assert {"username", "password", "api_key", "api_base", "model"} == set(props)
    # 描述中应引导模型先用 ask_user 收集缺失字段
    assert "ask_user" in by_name["create_user"].card.description


@pytest.mark.asyncio
async def test_delete_user_tool(state_file: Path):
    toolkit = ut.UserToolkit()
    await toolkit.create_user("alice", "pw", "sk-key")
    result = await toolkit.delete_user("alice")
    assert result["success"] is True
    assert us.get_user("alice") is None

    missing = await toolkit.delete_user("alice")
    assert missing["success"] is False
    assert "不存在" in missing["detail"]
