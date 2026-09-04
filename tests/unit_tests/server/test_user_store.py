"""user_store 单元测试。

覆盖：状态文件读写与损坏回退、create_user 的校验与重复检测、
list_users 的脱敏输出、get_user 的明文查询、web RPC handler。
"""

import asyncio
import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.user_registry import user_store as us


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把状态文件重定向到临时目录。"""
    path = tmp_path / "users.json"
    monkeypatch.setattr(us, "_state_file", lambda: path)
    return path


def test_read_state_missing_file(state_file: Path):
    assert us._read_state() == []


def test_read_state_broken_json(state_file: Path):
    state_file.write_text("{not json", encoding="utf-8")
    assert us._read_state() == []


def test_create_user_success(state_file: Path):
    result = us.create_user("alice", "secret-pw", "sk-1234567890", "https://api.example.com/v1", "gpt-x")
    assert result["success"] is True
    assert result["user"]["username"] == "alice"
    # 返回值中敏感字段必须是脱敏的
    assert result["user"]["password_masked"] != "secret-pw"
    assert "****" in result["user"]["password_masked"]
    assert result["user"]["api_key_masked"] != "sk-1234567890"
    assert "****" in result["user"]["api_key_masked"]
    assert result["user"]["api_base"] == "https://api.example.com/v1"
    assert result["user"]["model"] == "gpt-x"
    assert result["user"]["created_at"]

    # 落盘为明文（仅本地状态文件）
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert stored[0]["password"] == "secret-pw"
    assert stored[0]["api_key"] == "sk-1234567890"


def test_create_user_validation(state_file: Path):
    assert us.create_user("", "pw")["success"] is False
    assert us.create_user("alice", "")["success"] is False
    assert us.create_user("  ", "pw")["success"] is False


def test_create_user_duplicate(state_file: Path):
    assert us.create_user("alice", "pw1")["success"] is True
    result = us.create_user("alice", "pw2")
    assert result["success"] is False
    assert "已存在" in result["detail"]


def test_list_users_masks_secrets(state_file: Path):
    us.create_user("alice", "secret-pw", "sk-1234567890", "https://a", "m1")
    us.create_user("bob", "short", "", "", "")
    users = us.list_users()
    assert len(users) == 2
    alice = next(u for u in users if u["username"] == "alice")
    assert alice["password_masked"] == "sec****t-pw"
    assert alice["api_key_masked"] == "sk-****7890"
    bob = next(u for u in users if u["username"] == "bob")
    # 过短的密码全部掩码；空 api_key 不显示掩码
    assert bob["password_masked"] == "****"
    assert bob["api_key_masked"] == ""
    # 脱敏输出中不得出现明文
    assert "secret-pw" not in json.dumps(users)
    assert "sk-1234567890" not in json.dumps(users)


def test_get_user_returns_plaintext(state_file: Path):
    us.create_user("alice", "secret-pw", "sk-key", "https://a", "m1")
    record = us.get_user("alice")
    assert record is not None
    assert record["password"] == "secret-pw"
    assert record["api_key"] == "sk-key"
    assert us.get_user("nobody") is None
    assert us.get_user("") is None


def test_handle_users_list(state_file: Path):
    us.create_user("alice", "secret-pw", "sk-key", "https://a", "m1")
    payload = asyncio.run(us.handle_users_list({}))
    assert len(payload["users"]) == 1
    assert payload["users"][0]["username"] == "alice"
    assert "secret-pw" not in json.dumps(payload)


def test_handle_users_create(state_file: Path):
    payload = asyncio.run(us.handle_users_create({
        "username": "alice",
        "password": "secret-pw",
        "api_key": "sk-key",
        "api_base": "https://a",
        "model": "m1",
    }))
    assert payload["success"] is True
    assert us.get_user("alice") is not None

    # 缺密码 / 非 dict 参数
    assert asyncio.run(us.handle_users_create({"username": "bob"}))["success"] is False
    assert asyncio.run(us.handle_users_create(None))["success"] is False


def test_delete_user(state_file: Path):
    us.create_user("alice", "pw", "sk-key")
    us.create_user("bob", "pw2")
    result = us.delete_user("alice")
    assert result["success"] is True
    assert us.get_user("alice") is None
    assert us.get_user("bob") is not None
    # 落盘同步
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert [r["username"] for r in stored] == ["bob"]

    # 删除不存在的用户 / 空用户名
    assert us.delete_user("alice")["success"] is False
    assert us.delete_user("")["success"] is False


def test_handle_users_delete(state_file: Path):
    asyncio.run(us.handle_users_create({"username": "alice", "password": "pw"}))
    payload = asyncio.run(us.handle_users_delete({"username": "alice"}))
    assert payload["success"] is True
    assert us.get_user("alice") is None

    assert asyncio.run(us.handle_users_delete({"username": "alice"}))["success"] is False
    assert asyncio.run(us.handle_users_delete(None))["success"] is False
