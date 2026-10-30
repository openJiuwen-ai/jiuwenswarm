# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WeLink history polling: baseline, assistant-echo drop, and forward cursor."""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import build_command
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import (
    extract_login_prompt,
    parse_auth_status,
    parse_group_members,
    parse_history_messages,
    parse_recent_group_ids,
    reader_identity,
    select_new_messages,
    shares_reader_assistant,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_connect import auth_recheck_due
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_poller import CursorStore, MembershipStore, WelinkPoller


class _Result:
    def __init__(self, payload: object, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = json.dumps(payload)
        self.stderr = ""


def _shared_member_payload(account: str = "jiuwenswarmbot1") -> dict[str, object]:
    return {
        "respData": [
            {"memberAccount": account, "memberType": "TYPE_NORMAL"},
            {"memberAccount": f"dig_{reader_identity(account)}_assistant", "memberType": "TYPE_AGENT_ASSISTANT"},
        ]
    }


class _ScriptedCli:
    def __init__(self) -> None:
        self.history_calls: list[tuple[str, str | None]] = []
        self.member_calls: list[str] = []
        self._history = {
            None: [
                {"messageId": "10", "senderAccount": "jiuwenswarmbot1", "senderName": "李四", "text": "旧回复"},
                {"messageId": "11", "senderAccount": "00557917", "senderName": "张三", "text": "历史消息"},
            ],
            "11": [
                {"messageId": "11", "senderAccount": "00557917", "senderName": "张三", "text": "历史消息"},
                {"messageId": "12", "senderAccount": "jiuwenswarmbot1", "senderName": "李四", "text": "小九，今天天气怎么样"},
                {"messageId": "13", "senderAccount": "dig_jiuwenswarmbot1_assistant", "senderName": "助手", "text": "助手自己的话"},
                {"messageId": "14", "senderAccount": "00574873", "senderName": "赵六", "text": "新的问题"},
            ],
            "13": [
                {"messageId": "13", "senderAccount": "00574873", "senderName": "赵六", "text": "新的问题"},
            ],
        }

    def query_recent_conversations(self) -> _Result:
        return _Result({"conversations": [{"groupId": "g-1", "type": "group"}, {"userAccount": "a001", "type": "p2p"}]})

    def query_group_members(self, group_id: str) -> _Result:
        self.member_calls.append(group_id)
        return _Result(_shared_member_payload())

    def query_history(self, group_id: str, message_id: str | None) -> _Result:
        self.history_calls.append((group_id, message_id))
        return _Result({"messages": self._history.get(message_id, [])})


def test_recent_groups_skip_direct_chats() -> None:
    payload = {"data": [{"groupId": "42", "chatType": "group"}, {"groupId": "99", "chatType": "p2p"}]}
    assert parse_recent_group_ids(payload) == ["42"]


def test_recent_groups_read_welink_cli_conversation_info() -> None:
    payload = {
        "app_service_id": "1",
        "conversation_info": [
            {
                "group_id": "1022698754601669726",
                "group_name": "石瑞枫,黄宏运,聂光耀",
                "group_type": "NORMAL_GROUP",
                "recent_conversation_type": "CHAT_TYPE_GROUP_MSG",
            },
            {
                "group_id": "0",
                "recent_conversation_type": "CHAT_TYPE_P2P_MSG",
                "target_account": "s00576742",
            },
        ],
    }
    assert parse_recent_group_ids(payload) == ["1022698754601669726"]


def test_history_parser_reads_resp_data_chat_info() -> None:
    messages = parse_history_messages(
        {
            "respData": {
                "chatInfo": [
                    {
                        "msgId": 89629239958679573,
                        "sender": "s00576742",
                        "content": "1",
                        "contentType": "TEXT_MSG",
                    },
                    {
                        "msgId": 89629241149103423,
                        "sender": "s00576742",
                        "content": "小九在吗",
                        "contentType": "TEXT_MSG",
                        "groupId": "1022698754601669726",
                        "serverSendTime": 1767225600000,
                    },
                    {
                        "msgId": 89629241149103424,
                        "sender": "s00576742",
                        "content": "ignored.png",
                        "contentType": "IMAGE_MSG",
                    },
                ],
            },
            "resultCode": 0,
        },
        group_id="1022698754601669726",
    )
    assert [(item.message_id, item.text) for item in messages] == [
        ("89629239958679573", "1"),
        ("89629241149103423", "小九在吗"),
        ("89629241149103424", "[图片] ignored.png"),
    ]
    assert messages[1].sender_id == "s00576742"
    assert messages[1].received_at == "2026-01-01T00:00:00+00:00"
    assert messages[0].received_at == ""


def test_history_keeps_at_accounts_and_contact_mail() -> None:
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import (
        contact_mail_note,
        mention_accounts_in_history,
        parse_history_messages,
    )
    payload = {
        "respData": {"chatInfo": [{
            "msgId": 11,
            "sender": "h00557917",
            "content": "@小九 发给 @胡恩瑞",
            "contentType": "TEXT_MSG",
            "atAccountList": ["h001", "h001", "  "],
        }]},
    }
    messages = parse_history_messages(payload, group_id="g")
    assert messages[0].mention_accounts == ("h001",)
    assert mention_accounts_in_history(payload, "11") == ("h001",)
    note = contact_mail_note(("h001", "h002"), {"users": [{
        "personAccount": "h001",
        "w3Account": "h001",
        "personMail": "h001@huawei.com",
    }]})
    assert note == "被@账号邮箱：h001 h001@huawei.com"


def test_picture_and_file_markup_keep_name_size_and_dimensions() -> None:
    picture = (
        "/:um_begin{https://clouddrive.huawei.com/f/abc|Img|4692457|shore.jpg|0|4096;3072;deadbeef"
        "|isOriginalImg: 0;md5:abc}/:um_end"
    )
    messages = parse_history_messages(
        {
            "respData": {
                "chatInfo": [
                    {"msgId": 11, "sender": "h00557917", "content": picture, "contentType": "PICTURE_MSG"},
                    {
                        "msgId": 12,
                        "sender": "h00557917",
                        "content": "/:um_begin{https://clouddrive.huawei.com/f/def|File|2048|notes.pdf|0|0;0;abc}/:um_end 请看附件",
                        "contentType": "FILE_MSG",
                    },
                    {"msgId": 13, "sender": "h00557917", "content": "普通文字", "contentType": "TEXT_MSG"},
                ]
            }
        },
        group_id="1022698754601669726",
    )
    assert [item.text for item in messages] == [
        "[图片] shore.jpg 4096×3072 4.7MB",
        "[文件] notes.pdf 2.0KB\n请看附件",
        "普通文字",
    ]
    assert "clouddrive" not in messages[0].text
    assert "deadbeef" not in messages[0].text
    assert messages[0].media[0].url.endswith("/f/abc")
    assert messages[0].media[0].access_code == "deadbeef"
    assert messages[0].media[0].width == 4096
    assert messages[1].media[0].name == "notes.pdf"


def _seed_shared(directory: Path, group_id: str, account: str = "jiuwenswarmbot1", *, shared: bool = True) -> None:
    MembershipStore(directory / "membership.json").set(
        group_id,
        shared=shared,
        assistants={f"dig_{reader_identity(account)}_assistant"} if shared else set(),
    )


def test_rate_limit_moves_on_to_the_next_group(tmp_path: Path) -> None:
    cli = _ScriptedCli()
    calls: list[str] = []

    def recent() -> _Result:
        return _Result(
            {
                "conversation_info": [
                    {"group_id": "g-1", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
                    {"group_id": "g-2", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
                ]
            }
        )

    def history(group_id: str, message_id: str | None) -> _Result:
        calls.append(group_id)
        if group_id == "g-1":
            result = _Result({}, returncode=1)
            result.stderr = "Error: Query message history failed (status 429)"
            return result
        return _Result({"respData": {"chatInfo": [{"msgId": 8, "sender": "00574873", "content": "小九在吗", "contentType": "TEXT_MSG"}]}})

    cli.query_recent_conversations = recent
    cli.query_history = history
    cursors = CursorStore(tmp_path / "cursors.json")
    cursors.set("g-2", "7")
    _seed_shared(tmp_path, "g-1")
    _seed_shared(tmp_path, "g-2")
    poller = WelinkPoller(cli, "jiuwenswarmbot1", cursors, rate_limit_backoff_seconds=0)
    assert poller.poll() == []
    fresh = poller.poll()
    assert calls == ["g-1", "g-2"]
    assert [item.text for item in fresh] == ["小九在吗"]
    assert cli.member_calls == []


def test_history_backoff_still_classifies_an_unknown_group(tmp_path: Path) -> None:
    cli = _ScriptedCli()
    history_calls: list[str] = []

    def recent() -> _Result:
        return _Result(
            {
                "conversation_info": [
                    {"group_id": "g-1", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
                    {"group_id": "g-2", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
                ]
            }
        )

    def history(group_id: str, message_id: str | None) -> _Result:
        history_calls.append(group_id)
        result = _Result({}, returncode=1)
        result.stderr = "Error: Query message history failed (status 429)"
        return result

    cli.query_recent_conversations = recent
    cli.query_history = history
    _seed_shared(tmp_path, "g-1")
    poller = WelinkPoller(
        cli,
        "jiuwenswarmbot1",
        CursorStore(tmp_path / "cursors.json"),
        rate_limit_backoff_seconds=30,
        member_interval_seconds=0,
    )
    assert poller.poll() == []
    assert poller.poll() == []
    assert history_calls == ["g-1"]
    assert cli.member_calls == ["g-2"]


def test_one_history_call_follows_recent_order(tmp_path: Path) -> None:
    cli = _ScriptedCli()
    cli.query_recent_conversations = lambda: _Result(
        {
            "conversation_info": [
                {"group_id": "g-1", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
                {"group_id": "g-2", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
                {"group_id": "g-3", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
            ]
        }
    )
    calls: list[str] = []

    def history(group_id: str, message_id: str | None) -> _Result:
        calls.append(group_id)
        return _Result({"respData": {"chatInfo": [{"msgId": 5, "sender": "00574873", "content": "hi", "contentType": "TEXT_MSG"}]}})

    cli.query_history = history
    for group_id in ("g-1", "g-2", "g-3"):
        _seed_shared(tmp_path, group_id)
    poller = WelinkPoller(cli, "jiuwenswarmbot1", CursorStore(tmp_path / "cursors.json"))
    assert [item.text for item in poller.poll()] == ["hi"]
    assert calls == ["g-1"]
    assert [item.text for item in poller.poll()] == ["hi"]
    assert calls == ["g-1", "g-2"]
    assert [item.text for item in poller.poll()] == ["hi"]
    assert calls == ["g-1", "g-2", "g-3"]
    assert cli.member_calls == []


def test_history_parser_reads_cli_aliases() -> None:
    messages = parse_history_messages(
        {"messageList": [{"msgId": 7, "fromAccount": "00557917", "content": "你好"}]},
        group_id="g-1",
    )
    assert messages[0].message_id == "7"
    assert messages[0].sender_id == "00557917"
    assert messages[0].text == "你好"


def test_poll_baselines_then_delivers_the_reader_and_other_people(tmp_path: Path) -> None:
    cli = _ScriptedCli()
    poller = WelinkPoller(cli, "jiuwenswarmbot1", CursorStore(tmp_path / "cursors.json"))

    assert poller.poll() == []
    assert cli.history_calls == []
    assert cli.member_calls == ["g-1"]

    opened = poller.poll()
    assert [item.message_id for item in opened] == ["10", "11"]
    assert cli.history_calls == [("g-1", None)]
    poller.commit(opened[-1].group_id, opened[-1].message_id)

    fresh = poller.poll()
    assert [item.message_id for item in fresh] == ["12", "14"]
    assert [item.text for item in fresh] == ["小九，今天天气怎么样", "新的问题"]
    poller.commit(fresh[-1].group_id, fresh[-1].message_id)

    assert poller.poll() == []
    assert cli.history_calls[-1] == ("g-1", "14")


def test_startup_delivers_a_short_backlog(tmp_path: Path) -> None:
    pages = {
        "1": [
            {"messageId": "2", "senderAccount": "human", "text": "旧消息甲"},
            {"messageId": "3", "senderAccount": "human", "text": "旧消息乙"},
        ],
        "3": [
            {"messageId": "4", "senderAccount": "human", "text": "刚到的消息"},
        ],
    }

    class _Cli:
        def query_recent_conversations(self) -> _Result:
            return _Result({"conversations": [{"groupId": "g-1", "type": "group"}]})

        def query_group_members(self, group_id: str) -> _Result:
            return _Result(_shared_member_payload())

        def query_history(self, group_id: str, message_id: str | None) -> _Result:
            return _Result({"messages": pages.get(message_id or "", [])})

    store = CursorStore(tmp_path / "cursors.json")
    store.set("g-1", "1")
    _seed_shared(tmp_path, "g-1")
    poller = WelinkPoller(_Cli(), "jiuwenswarmbot1", store)

    fresh = poller.poll()
    assert [item.text for item in fresh] == ["旧消息甲", "旧消息乙"]
    assert store.get("g-1") == "1"
    for item in fresh:
        poller.commit(item.group_id, item.message_id)
    assert store.get("g-1") == "3"
    assert [item.text for item in poller.poll()] == ["刚到的消息"]


def test_long_backlog_keeps_only_the_newest_five(tmp_path: Path) -> None:
    page = [
        {"messageId": str(index), "senderAccount": "human", "text": f"消息{index}"}
        for index in range(2, 10)
    ]

    class _Cli:
        def query_recent_conversations(self) -> _Result:
            return _Result({"conversations": [{"groupId": "g-1", "type": "group"}]})

        def query_group_members(self, group_id: str) -> _Result:
            return _Result(_shared_member_payload())

        def query_history(self, group_id: str, message_id: str | None) -> _Result:
            if message_id == "9":
                return _Result({"messages": []})
            return _Result({"messages": page})

    store = CursorStore(tmp_path / "cursors.json")
    store.set("g-1", "1")
    _seed_shared(tmp_path, "g-1")
    poller = WelinkPoller(_Cli(), "jiuwenswarmbot1", store)

    fresh = poller.poll()
    assert [item.message_id for item in fresh] == ["5", "6", "7", "8", "9"]
    poller.commit(fresh[-1].group_id, fresh[-1].message_id)
    assert poller.poll() == []


def test_only_groups_shared_with_this_assistant_are_read(tmp_path: Path) -> None:
    members = parse_group_members(
        {
            "respData": [
                {"memberAccount": "h00557917", "memberType": "TYPE_NORMAL"},
                {"memberAccount": "dig_00574873_other", "memberType": "TYPE_AGENT_ASSISTANT"},
                {"memberAccount": "dig_00557917_mine", "memberType": "TYPE_AGENT_ASSISTANT"},
            ]
        }
    )
    assert shares_reader_assistant(members, "h00557917") is True
    foreign = [item for item in members if item.account != "dig_00557917_mine"]
    assert shares_reader_assistant(foreign, "h00557917") is False

    cli = _ScriptedCli()
    cli.query_recent_conversations = lambda: _Result(
        {
            "conversation_info": [
                {"group_id": "shared", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
                {"group_id": "alone", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
            ]
        }
    )

    def members_for(group_id: str) -> _Result:
        cli.member_calls.append(group_id)
        if group_id == "shared":
            return _Result(_shared_member_payload("h00557917"))
        return _Result(
            {
                "respData": [
                    {"memberAccount": "h00557917", "memberType": "TYPE_NORMAL"},
                    {"memberAccount": "dig_00574873_other", "memberType": "TYPE_AGENT_ASSISTANT"},
                ]
            }
        )

    cli.query_group_members = members_for

    def history(group_id: str, message_id: str | None) -> _Result:
        cli.history_calls.append((group_id, message_id))
        return _Result(
            {
                "respData": {
                    "chatInfo": [
                        {"msgId": 3, "sender": "dig_00557917_assistant", "content": "助手自己的话", "contentType": "TEXT_MSG"},
                        {"msgId": 4, "sender": "00574873", "content": "请助手看一下", "contentType": "TEXT_MSG"},
                    ]
                }
            }
        )

    cli.query_history = history
    store = CursorStore(tmp_path / "cursors.json")
    store.set("shared", "2")
    store.set("alone", "1")
    _seed_shared(tmp_path, "shared", "h00557917")
    _seed_shared(tmp_path, "alone", "h00557917", shared=False)
    poller = WelinkPoller(cli, "h00557917", store)
    fresh = poller.poll()
    assert cli.history_calls == [("shared", "2")]
    assert cli.member_calls == []
    assert [item.text for item in fresh] == ["请助手看一下"]

    cold = _ScriptedCli()
    cold.query_recent_conversations = cli.query_recent_conversations
    cold.query_group_members = members_for
    cold_dir = tmp_path / "cold"
    cold_dir.mkdir()
    cold_store = CursorStore(cold_dir / "cursors.json")
    cold_store.set("shared", "2")
    cli.member_calls.clear()
    WelinkPoller(cold, "h00557917", cold_store, member_interval_seconds=0).poll()
    assert cli.member_calls == ["shared"]
    assert cold.history_calls == []


def test_stale_not_shared_mark_updates_and_catches_up_the_latest_page(tmp_path: Path) -> None:
    recent = [
        {"group_id": "g-front", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
        {"group_id": "g-stale", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
    ]
    latest = [
        {"messageId": "4", "senderAccount": "h00557917", "text": "旧问题"},
        {"messageId": "6", "senderAccount": "h00557917", "text": "问题一"},
        {"messageId": "7", "senderAccount": "dig_00557917_assistant", "text": "助手自己的话"},
        {"messageId": "8", "senderAccount": "00574873", "text": "问题二"},
        {"messageId": "9", "senderAccount": "h00557917", "text": "问题三"},
        {"messageId": "10", "senderAccount": "h00557917", "text": "问题四"},
        {"messageId": "11", "senderAccount": "h00557917", "text": "问题五"},
        {"messageId": "12", "senderAccount": "h00557917", "text": "问题六"},
    ]
    cli = _ScriptedCli()
    cli.query_recent_conversations = lambda: _Result({"conversation_info": recent})

    def members_for(group_id: str) -> _Result:
        cli.member_calls.append(group_id)
        return _Result(_shared_member_payload("h00557917"))

    def history(group_id: str, message_id: str | None) -> _Result:
        cli.history_calls.append((group_id, message_id))
        if group_id == "g-stale" and message_id is None:
            return _Result({"messages": latest})
        return _Result({"messages": []})

    cli.query_group_members = members_for
    cli.query_history = history
    store = CursorStore(tmp_path / "cursors.json")
    store.set("g-front", "1")
    store.set("g-stale", "5")
    _seed_shared(tmp_path, "g-front", "h00557917")
    _seed_shared(tmp_path, "g-stale", "h00557917", shared=False)
    poller = WelinkPoller(
        cli,
        "h00557917",
        store,
        member_interval_seconds=0,
        membership_recheck_seconds=100,
    )

    assert poller.poll() == []
    assert cli.history_calls == [("g-front", "1")]
    assert cli.member_calls == []

    assert poller.poll() == []
    assert cli.member_calls == ["g-stale"]
    assert MembershipStore(tmp_path / "membership.json").get("g-stale")[0] is True

    caught_up = poller.poll()
    assert cli.history_calls[-1] == ("g-stale", None)
    assert [item.text for item in caught_up] == ["问题一", "问题二", "问题三", "问题四", "问题五", "问题六"]
    poller.commit(caught_up[-1].group_id, caught_up[-1].message_id)

    recent[:] = [
        {"group_id": "g-stale", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
        {"group_id": "g-front", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
    ]
    assert poller.poll() == []
    assert cli.history_calls[-1] == ("g-stale", "12")


def test_not_shared_group_waits_out_the_recheck_cooldown(tmp_path: Path) -> None:
    cli = _ScriptedCli()
    cli.query_recent_conversations = lambda: _Result(
        {"conversation_info": [{"group_id": "g-stale", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"}]}
    )
    def members(group_id: str) -> _Result:
        cli.member_calls.append(group_id)
        return _Result({"respData": [{"memberAccount": "h00557917", "memberType": "TYPE_NORMAL"}]})

    cli.query_group_members = members
    _seed_shared(tmp_path, "g-stale", "h00557917", shared=False)
    poller = WelinkPoller(
        cli,
        "h00557917",
        CursorStore(tmp_path / "cursors.json"),
        member_interval_seconds=0,
        membership_recheck_seconds=100,
    )

    assert poller.poll() == []
    assert poller.poll() == []
    assert cli.member_calls == ["g-stale"]
    assert cli.history_calls == []
    assert MembershipStore(tmp_path / "membership.json").get("g-stale")[0] is False


def test_recheck_rotates_through_later_groups_before_repeating_the_front(tmp_path: Path) -> None:
    groups = [f"g-{index}" for index in range(4)]
    cli = _ScriptedCli()
    cli.query_recent_conversations = lambda: _Result(
        {
            "conversation_info": [
                {"group_id": group_id, "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"}
                for group_id in groups
            ]
        }
    )

    def members(group_id: str) -> _Result:
        cli.member_calls.append(group_id)
        return _Result({"respData": [{"memberAccount": "h00557917", "memberType": "TYPE_NORMAL"}]})

    cli.query_group_members = members
    for group_id in groups:
        _seed_shared(tmp_path, group_id, "h00557917", shared=False)
    poller = WelinkPoller(
        cli,
        "h00557917",
        CursorStore(tmp_path / "cursors.json"),
        member_interval_seconds=0,
        membership_recheck_seconds=0,
    )

    for _ in groups:
        assert poller.poll() == []
    assert cli.member_calls == groups


def test_incomplete_member_list_is_not_stored_as_not_shared(tmp_path: Path) -> None:
    cli = _ScriptedCli()
    cli.query_recent_conversations = lambda: _Result(
        {"conversation_info": [{"group_id": "g-stale", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"}]}
    )
    cli.query_group_members = lambda group_id: _Result(
        {
            "resultCode": 0,
            "respData": [{"memberAccount": "someone-else", "memberType": "TYPE_NORMAL"}],
        }
    )
    poller = WelinkPoller(
        cli,
        "h00557917",
        CursorStore(tmp_path / "cursors.json"),
        member_interval_seconds=0,
    )

    assert poller.poll() == []
    assert MembershipStore(tmp_path / "membership.json").get("g-stale") is None


def test_not_shared_group_is_rechecked_when_it_moves_forward(tmp_path: Path) -> None:
    recent = [{"group_id": "g-stale", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"}]
    cli = _ScriptedCli()
    cli.query_recent_conversations = lambda: _Result({"conversation_info": list(recent)})
    cli.query_group_members = lambda group_id: (
        cli.member_calls.append(group_id) or _Result(
            {"respData": [{"memberAccount": "h00557917", "memberType": "TYPE_NORMAL"}]}
        )
    )
    cli.query_history = lambda group_id, message_id: (
        cli.history_calls.append((group_id, message_id)) or _Result({"messages": []})
    )
    store = CursorStore(tmp_path / "cursors.json")
    store.set("g-front", "1")
    _seed_shared(tmp_path, "g-stale", "h00557917", shared=False)
    _seed_shared(tmp_path, "g-front", "h00557917")
    poller = WelinkPoller(
        cli,
        "h00557917",
        store,
        member_interval_seconds=0,
        membership_recheck_seconds=100,
    )

    assert poller.poll() == []
    assert cli.member_calls == ["g-stale"]

    recent[:] = [
        {"group_id": "g-front", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
        {"group_id": "g-stale", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
    ]
    assert poller.poll() == []
    assert poller.poll() == []
    assert cli.member_calls == ["g-stale"]

    recent[:] = [
        {"group_id": "g-stale", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
        {"group_id": "g-front", "recent_conversation_type": "CHAT_TYPE_GROUP_MSG"},
    ]
    assert poller.poll() == []
    assert poller.poll() == []
    assert cli.member_calls == ["g-stale", "g-stale"]


def test_membership_survives_a_restart(tmp_path: Path) -> None:
    first_cli = _ScriptedCli()
    store = CursorStore(tmp_path / "cursors.json")
    WelinkPoller(first_cli, "jiuwenswarmbot1", store).poll()
    assert first_cli.member_calls == ["g-1"]

    second_cli = _ScriptedCli()
    store.set("g-1", "11")
    fresh = WelinkPoller(second_cli, "jiuwenswarmbot1", store).poll()
    assert second_cli.member_calls == []
    assert [item.message_id for item in fresh] == ["12", "14"]


def test_auth_status_is_not_checked_again_within_ten_minutes() -> None:
    assert auth_recheck_due(0, 100) is True
    assert auth_recheck_due(100, 200) is False
    assert auth_recheck_due(100, 700) is True


def test_first_sight_of_a_group_delivers_its_first_message(tmp_path: Path) -> None:
    class _Cli:
        def query_recent_conversations(self) -> _Result:
            return _Result({"conversations": [{"groupId": "g-1", "type": "group"}]})

        def query_group_members(self, group_id: str) -> _Result:
            return _Result(_shared_member_payload("h00557917"))

        def query_history(self, group_id: str, message_id: str | None) -> _Result:
            if message_id:
                return _Result({"messages": []})
            return _Result(
                {
                    "messages": [
                        {"messageId": "2", "senderAccount": "dig_00557917_assistant", "text": "助手自己的话"},
                        {"messageId": "3", "senderAccount": "h00557917", "text": "小九，给我写首诗"},
                    ]
                }
            )

    _seed_shared(tmp_path, "g-1", "h00557917")
    poller = WelinkPoller(_Cli(), "h00557917", CursorStore(tmp_path / "cursors.json"))
    fresh = poller.poll()
    assert [item.text for item in fresh] == ["小九，给我写首诗"]


def test_select_new_messages_keeps_the_reader_and_drops_the_assistant() -> None:
    messages = parse_history_messages(
        [
            {"messageId": "2", "senderAccount": "h00557917", "text": "小九，今天天气怎么样"},
            {"messageId": "3", "senderAccount": "dig_00557917_assistant", "text": "助手自己的话"},
            {"messageId": "4", "senderAccount": "human", "text": "hi"},
        ],
        group_id="g",
    )
    newer, cursor = select_new_messages(
        messages,
        "1",
        account="h00557917",
        ignore_senders={"dig_00557917_assistant"},
    )
    assert [item.message_id for item in newer] == ["2", "4"]
    assert cursor == "4"


def test_npm_shim_sends_multiline_text_to_the_native_executable(tmp_path: Path) -> None:
    shim = tmp_path / "welink-cli.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    exe = tmp_path / "node_modules" / "@welink" / "welink-cli" / "welink-cli.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    poem = "第一句\n\n《晚风说》\n晚风绕过窗台"

    command = build_command(str(shim), ["im", "query-history-message", "--group-id", "g-1", "--text", poem])

    assert command[0] == str(exe)
    assert command[-1] == poem
    assert "cmd" not in command


def test_auth_status_and_qr_prompt() -> None:
    logged_in, account = parse_auth_status('{"loggedIn": true, "account": "jiuwenswarmbot1"}')
    assert logged_in is True
    assert account == "jiuwenswarmbot1"
    logged_out, _ = parse_auth_status("not logged in")
    assert logged_out is False
    cli_status = "Configuration:\n  Environment:  pro\n\nCredentials:\n  User Token:   abc123 (expires in 26m11s)\n  UID:          h00557917\n"
    logged_in, account = parse_auth_status(cli_status)
    assert logged_in is True
    assert account == "h00557917"
    logged_out, account = parse_auth_status(
        "Credentials:\n  Status:       not authenticated\n  Hint:         Run 'welink-cli auth login' to authenticate.\n"
    )
    assert logged_out is False
    assert account == ""
    url, qr = extract_login_prompt("open https://login.welink.example/oauth\n" + "█" * 12)
    assert "login.welink.example" in url
    assert "█" in qr


def test_switching_account_logs_out_before_showing_a_new_qr(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import WelinkCliResult

    events: list[str] = []

    class FakeCli:
        def __init__(self, cli_path: str, credential_dir: Path, *, env_name: str = "pro") -> None:
            self.credential_dir = credential_dir

        def auth_status(self) -> WelinkCliResult:
            events.append("status")
            text = "Credentials:\n  User Token:   abc123\n  UID:          h00557917\n"
            return WelinkCliResult(returncode=0, stdout=text, stderr="")

        def run(self, args: list[str], timeout: float = 30) -> WelinkCliResult:
            events.append(" ".join(args))
            return WelinkCliResult(returncode=0, stdout="logged out", stderr="")

    class FakePopen:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            events.append("login")
            self.stdout = None

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(welink_login, "WelinkCli", FakeCli)
    monkeypatch.setattr(welink_login.subprocess, "Popen", FakePopen)
    session = welink_login.WelinkLoginManager().start(
        agent_id="mate_xiaojiu",
        account="p_openjiuwenbot1",
        credential_dir=tmp_path,
        cli_path="welink-cli",
        env_name="pro",
    )

    assert session.status == "pending"
    assert session.error == ""
    assert events == ["status", "auth logout", "login"]

    same = welink_login.WelinkLoginManager()

    class SameAccountCli(FakeCli):
        def auth_status(self) -> WelinkCliResult:
            events.append("same-status")
            text = "Credentials:\n  User Token:   abc123\n  UID:          p_openjiuwenbot1\n"
            return WelinkCliResult(returncode=0, stdout=text, stderr="")

    monkeypatch.setattr(welink_login, "WelinkCli", SameAccountCli)
    events.clear()
    same_session = same.start(
        agent_id="mate_xiaojiu",
        account="p_openjiuwenbot1",
        credential_dir=tmp_path,
        cli_path="welink-cli",
        env_name="pro",
    )
    assert same_session.status == "pending"
    assert events == ["same-status", "login"]


def test_scan_without_a_declared_account_uses_the_phone_that_scans(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import WelinkCliResult

    events: list[str] = []

    class FakeCli:
        def __init__(self, cli_path: str, credential_dir: Path, *, env_name: str = "pro") -> None:
            return None

        def auth_status(self) -> WelinkCliResult:
            events.append("status")
            text = "Credentials:\n  User Token:   abc123\n  UID:          h00557917\n"
            return WelinkCliResult(returncode=0, stdout=text, stderr="")

        def run(self, args: list[str], timeout: float = 30) -> WelinkCliResult:
            events.append(" ".join(args))
            return WelinkCliResult(returncode=0, stdout="logged out", stderr="")

    class FakePopen:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            events.append("login")
            self.stdout = None

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(welink_login, "WelinkCli", FakeCli)
    monkeypatch.setattr(welink_login.subprocess, "Popen", FakePopen)
    session = welink_login.WelinkLoginManager().start(
        agent_id="mate_xiaojiu",
        account="",
        credential_dir=tmp_path,
        cli_path="welink-cli",
        env_name="pro",
    )
    assert session.status == "pending"
    assert session.replaced_account == "h00557917"
    assert events == ["status", "auth logout", "login"]

    accepted = welink_login.WelinkLoginSession(
        session_id="scan",
        agent_id="mate_xiaojiu",
        account="",
        credential_dir=tmp_path,
    )
    assert welink_login._accept_login(accepted, True, "h00557917") is True
    assert accepted.status == "success"
    assert accepted.resolved_account == "h00557917"
    unidentified = welink_login.WelinkLoginSession(
        session_id="blank",
        agent_id="mate_xiaojiu",
        account="",
        credential_dir=tmp_path,
    )
    assert welink_login._accept_login(unidentified, True, "") is True
    assert unidentified.status == "failed"
    assert "没有识别到 WeLink 账号" in unidentified.error


def test_second_start_reuses_the_pending_qr(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_login
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import WelinkCliResult

    events: list[str] = []

    class FakeCli:
        def __init__(self, cli_path: str, credential_dir: Path, *, env_name: str = "pro") -> None:
            return None

        def auth_status(self) -> WelinkCliResult:
            events.append("status")
            return WelinkCliResult(returncode=0, stdout="Status:       not authenticated\n", stderr="")

        def run(self, args: list[str], timeout: float = 30) -> WelinkCliResult:
            events.append(" ".join(args))
            return WelinkCliResult(returncode=0, stdout="", stderr="")

    class FakePopen:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            events.append("login")
            self.stdout = None

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            events.append("terminate")

        def wait(self) -> int:
            return 1

    monkeypatch.setattr(welink_login, "WelinkCli", FakeCli)
    monkeypatch.setattr(welink_login.subprocess, "Popen", FakePopen)
    manager = welink_login.WelinkLoginManager()
    first = manager.start(
        agent_id="mate_xiaojiu",
        account="p_openjiuwenbot1",
        credential_dir=tmp_path,
        cli_path="welink-cli",
        env_name="pro",
    )
    events.clear()
    second = manager.start(
        agent_id="mate_xiaojiu",
        account="p_openjiuwenbot1",
        credential_dir=tmp_path,
        cli_path="welink-cli",
        env_name="pro",
    )
    assert second.session_id == first.session_id
    assert second.status == "pending"
    assert events == []
    assert welink_login._login_failure_message("Polling error: HTTP 500\n", 1).startswith("授权码状态查询失败")
    stopped = welink_login._login_failure_message("Waiting for scan...\n", 1)
    assert "退出码 1" in stopped
