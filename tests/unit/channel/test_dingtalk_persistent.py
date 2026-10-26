"""DingTalk JM transport contracts; no server, model, credentials or real network."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.configuration import (
    allow_from_valid,
    dingtalk_apps,
    dingtalk_channel_settings,
    merge_dingtalk_config,
    update_dingtalk_section,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.dingtalk_connect import (
    DingTalkChannel,
    DingTalkConfig,
    DingTalkHandler,
)
from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import DingTalkInboundEvent, configured_dingtalk_bindings

BASES = {"api_base": "https://api.example.invalid", "oapi_base": "https://oapi.example.invalid"}


def _callback(**changes):
    return {
        "conversationType": "1", "conversationId": "conversation-test", "msgtype": "text",
        "msgId": "message-test", "senderStaffId": "staff-test", "senderId": "opaque-test",
        "senderNick": "Test sender", "robotCode": "robot-test", "text": {"content": "test task"},
        "sessionWebhook": "https://example.invalid/temporary", **changes,
    }


def _channel():
    channel = DingTalkChannel(DingTalkConfig(
        client_id="app-test", client_secret="fake-secret", robot_code="robot-test", agent_id="agent-dingtalk",
        is_default=False, api_base="https://api.example.invalid", allow_from=["staff-test"],
    ), MagicMock(spec=RobotMessageRouter))
    channel._http = AsyncMock(spec=httpx.AsyncClient)
    channel._access_token = "fake-token"
    channel._token_expiry = time.time() + 3600
    return channel


async def _send(channel, **changes):
    return await channel.send_persistent_result(
        chat_id="staff-test", text="test result", source_message_id="message-test", idempotency_key="delivery-test",
        **changes,
    )


def test_empty_yaml_values_are_typed_before_channels_start():
    raw = {
        "client_id": "app", "client_secret": "fake-secret", "enabled": None, "robot_code": None,
        "persistent_agent_id": None, "allow_from": None, "api_base": None,
    }
    app, = dingtalk_apps(raw)
    assert app["robot_code"] == app["persistent_agent_id"] == "" and app["allow_from"] == []
    assert app["enabled"] is True and app["is_default"] is True
    settings, = dingtalk_channel_settings(raw, **BASES)
    assert DingTalkConfig.model_validate(settings).api_base == BASES["api_base"]
    assert dingtalk_channel_settings({"client_id": "app", "client_secret": None}, **BASES) == []


@pytest.mark.parametrize("allow_from", ["staff-only", [123456], ["staff-a", None], {"staff-a": True}])
def test_a_malformed_allowlist_never_admits_everyone(allow_from):
    raw = {"client_id": "app", "client_secret": "fake-secret", "allow_from": allow_from}
    app, = dingtalk_apps(raw)
    assert app["allow_from"] == allow_from and not allow_from_valid(app)
    assert dingtalk_channel_settings(raw, **BASES) == []
    with pytest.raises(ValueError, match="allow_from"):
        merge_dingtalk_config({}, {"apps": [raw]})


def test_a_partial_app_edit_keeps_its_enabled_state_and_api_base():
    current = {"apps": [{"client_id": "app", "client_secret": "fake-secret", "enabled": True,
                         "api_base": "https://app.example.invalid", "allow_from": ["staff-a"]}]}
    app, = merge_dingtalk_config(current, {"apps": [{"client_id": "app"}]})["apps"]
    assert app["enabled"] is True and app["client_secret"] == "fake-secret"
    assert app["api_base"] == "https://app.example.invalid" and app["allow_from"] == ["staff-a"]


def test_only_one_ordinary_app_and_no_duplicates_start():
    raw = {"apps": [
        {"client_id": "ordinary", "client_secret": "fake"},
        {"client_id": "second-ordinary", "client_secret": "fake"},
        {"client_id": "twin", "client_secret": "fake", "persistent_agent_id": "agent-a"},
        {"client_id": "twin", "client_secret": "fake", "persistent_agent_id": "agent-b"},
        {"client_id": "jm", "client_secret": "fake", "persistent_agent_id": "agent-c", "enabled": False},
    ]}
    assert [item["client_id"] for item in dingtalk_channel_settings(raw, **BASES)] == ["ordinary"]


def test_settings_edit_moves_legacy_credentials_into_apps_and_keeps_what_it_omits():
    old = {
        "enabled": True, "client_id": "ordinary", "client_secret": "fake-old",
        "api_base": "https://example.invalid", "last_sender_id": "staff-old",
    }
    bound = merge_dingtalk_config(old, {"apps": [
        *dingtalk_apps(old),
        {"client_id": "jm", "client_secret": "fake-jm", "persistent_agent_id": "agent-dingtalk", "robot_code": "r",
         "allow_from": []},
    ]})
    edited = merge_dingtalk_config(bound, {"apps": [
        {"client_id": "ordinary", "allow_from": ["staff-new"], "enabled": True},
        {"client_id": "jm", "enabled": True},
    ]})
    ordinary, jm = edited["apps"]
    assert "client_id" not in edited and "client_secret" not in edited
    assert ordinary["client_secret"] == "fake-old" and ordinary["allow_from"] == ["staff-new"]
    assert jm["client_secret"] == "fake-jm" and jm["persistent_agent_id"] == "agent-dingtalk" and jm["robot_code"] == "r"
    assert edited["api_base"] == old["api_base"] and edited["last_sender_id"] == "staff-old"
    assert ordinary["is_default"] and not jm["is_default"]
    assert "apps" not in old


def test_secret_rotation_keeps_cron_identity_but_another_robot_does_not():
    old = {"apps": [{"client_id": "ordinary", "client_secret": "fake-old"}], "last_sender_id": "staff-old",
           "last_conversation_id": "chat-old"}
    rotated = merge_dingtalk_config(old, {"apps": [{"client_id": "ordinary", "client_secret": "fake-new"}]})
    assert rotated["last_sender_id"] == "staff-old" and rotated["apps"][0]["client_secret"] == "fake-new"
    replaced = merge_dingtalk_config(old, {"apps": [
        {"client_id": "other", "client_secret": "fake-other", "allow_from": []},
    ]})
    assert "last_sender_id" not in replaced and "last_conversation_id" not in replaced


def test_deleting_the_ordinary_app_never_promotes_a_jm_robot_to_default():
    current = {"apps": [
        {"client_id": "ordinary", "client_secret": "fake"},
        {"client_id": "jm", "client_secret": "fake", "persistent_agent_id": "agent-dingtalk"},
    ], "last_sender_id": "staff-old"}
    deleted = merge_dingtalk_config(current, {"apps": [{"client_id": "jm"}]})
    assert [app["client_id"] for app in deleted["apps"]] == ["jm"]
    assert deleted["apps"][0]["persistent_agent_id"] == "agent-dingtalk" and not deleted["apps"][0]["is_default"]
    assert "last_sender_id" not in deleted


def test_a_converted_default_app_stays_default_and_keeps_its_cron_identity():
    current = {"apps": [{"client_id": "ordinary", "client_secret": "fake"}], "last_sender_id": "staff-old"}
    converted = merge_dingtalk_config(current, {"apps": [
        {"client_id": "ordinary", "persistent_agent_id": "agent-dingtalk"},
    ]})
    assert converted["apps"][0]["is_default"] and converted["last_sender_id"] == "staff-old"


def test_app_list_rejects_duplicates_a_second_ordinary_app_and_missing_ids():
    old = {"apps": [{"client_id": "app", "client_secret": "fake-old", "persistent_agent_id": "agent-dingtalk"}]}
    with pytest.raises(ValueError, match="duplicate"):
        merge_dingtalk_config(old, {"apps": [{"client_id": "app"}, {"client_id": "app"}]})
    with pytest.raises(ValueError, match="only one ordinary"):
        merge_dingtalk_config(old, {"apps": [{"client_id": "one", "allow_from": []}, {"client_id": "two", "allow_from": []}]})
    with pytest.raises(ValueError, match="client_id"):
        merge_dingtalk_config(old, {"apps": [{"client_id": " "}]})
    with pytest.raises(ValueError, match="apps"):
        merge_dingtalk_config(old, {"enabled": False, "client_id": "", "client_secret": ""})


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)

    def channels():
        return config_module.load_yaml_round_trip(path)["channels"]

    return SimpleNamespace(path=path, module=config_module, channels=channels)


def test_saving_merges_with_the_file_and_removes_dropped_keys(config_file):
    config_file.module.dump_yaml_round_trip(config_file.path, {"channels": {
        "dingtalk": {"client_id": "old", "client_secret": "fake-old", "last_sender_id": "staff-old"},
        "telegram": {"enabled": False},
    }})
    saved = update_dingtalk_section(lambda current: {"apps": [
        {"client_id": "other", "client_secret": "fake-other", "allow_from": []},
    ]})
    stored = config_file.channels()
    assert "last_sender_id" not in stored["dingtalk"] and "client_id" not in stored["dingtalk"]
    assert stored["dingtalk"]["apps"][0]["client_id"] == "other" == saved["apps"][0]["client_id"]
    assert stored["telegram"] == {"enabled": False}


def test_a_save_keeps_the_cron_identity_the_ordinary_channel_wrote_after_load(config_file):
    config_file.module.dump_yaml_round_trip(config_file.path, {"channels": {
        "dingtalk": {"apps": [{"client_id": "ordinary", "client_secret": "fake"}]},
    }})
    # The ordinary channel records its last sender in the file only, never in Gateway memory.
    config_file.module.update_channel_in_config("dingtalk", {"last_sender_id": "staff-new"})
    update_dingtalk_section(lambda current: {"apps": [{"client_id": "ordinary", "allow_from": ["staff-new"]}]})
    stored = config_file.channels()["dingtalk"]
    assert stored["last_sender_id"] == "staff-new" and stored["apps"][0]["allow_from"] == ["staff-new"]


def test_a_rejected_save_leaves_the_file_unchanged(config_file):
    config_file.module.dump_yaml_round_trip(config_file.path, {"channels": {
        "dingtalk": {"apps": [{"client_id": "ordinary", "client_secret": "fake"}]},
    }})

    def refuse(section):
        raise ValueError("refused by test")

    with pytest.raises(ValueError, match="refused"):
        update_dingtalk_section(lambda current: {"apps": [{"client_id": "other", "allow_from": []}]}, validate=refuse)
    assert [app["client_id"] for app in config_file.channels()["dingtalk"]["apps"]] == ["ordinary"]


def test_an_unrelated_edit_keeps_a_malformed_allowlist_and_its_app_stopped():
    current = {"apps": [
        {"client_id": "ordinary", "client_secret": "fake", "allow_from": [None]},
        {"client_id": "jm", "client_secret": "fake", "persistent_agent_id": "agent-a", "name": "Old"},
    ]}
    edited = merge_dingtalk_config(current, {"apps": [{"client_id": "ordinary"}, {"client_id": "jm", "name": "New"}]})
    ordinary, jm = edited["apps"]
    assert ordinary["allow_from"] == [None] and jm["name"] == "New"
    assert [item["client_id"] for item in dingtalk_channel_settings(edited, **BASES)] == ["jm"]


def test_a_new_app_must_state_its_allowlist():
    current = {"apps": [{"client_id": "old", "client_secret": "fake", "allow_from": "staff-only"}]}
    # Another client_id is a new app: an omitted allowlist has nothing to keep and must not mean "everyone".
    with pytest.raises(ValueError, match="allow_from is required"):
        merge_dingtalk_config(current, {"apps": [{"client_id": "new", "client_secret": "fake"}]})
    renamed = merge_dingtalk_config(current, {"apps": [
        {"client_id": "new", "client_secret": "fake", "allow_from": ["staff-a"]},
    ]})
    assert [item["allow_from"] for item in dingtalk_channel_settings(renamed, **BASES)] == [["staff-a"]]


def test_bindings_list_each_enabled_app_once_and_leave_reuse_to_the_connection_check():
    config = {"channels": {
        "feishu": {"apps": [{"app_id": "feishu-app", "persistent_agent_id": "same-agent"}]},
        "dingtalk": {"apps": [
            {"client_id": "dingtalk-app", "client_secret": "fake", "persistent_agent_id": "same-agent"},
            {"client_id": "stopped-app", "client_secret": "fake", "persistent_agent_id": "agent-b", "enabled": False},
        ]},
    }}
    # Whether "same-agent" may serve this app is decided by its recorded connection at inbound.
    binding, = configured_dingtalk_bindings(config)
    assert (binding.app_id, binding.agent_id) == ("dingtalk-app", "same-agent")


def test_event_preserves_business_identity_and_excludes_temporary_webhook():
    event = DingTalkInboundEvent.from_callback(_callback(), app_id="trusted-app", agent_id="agent-dingtalk")
    payload = event.to_payload()
    assert payload["receiving_app_id"] == "trusted-app"
    assert payload["message_id"] == "message-test"
    assert payload["sender_staff_id"] == "staff-test"
    assert "sessionWebhook" not in payload and "senderId" not in payload
    assert DingTalkInboundEvent.from_payload(payload) == event


@pytest.mark.parametrize("changes", [
    {"senderStaffId": None}, {"conversationId": ""}, {"msgId": None},
    {"conversationType": None}, {"conversationType": "2"}, {"msgtype": "file"}, {"text": {}},
])
def test_event_never_guesses_missing_identity_or_unsupported_input(changes):
    with pytest.raises(ValueError):
        DingTalkInboundEvent.from_callback(_callback(**changes), app_id="app-test", agent_id="agent-dingtalk")


@pytest.mark.asyncio
async def test_success_requires_real_receipt_and_correct_robot_target():
    channel = _channel()
    channel._http.post.return_value = httpx.Response(200, json={"processQueryKey": "opaque+/send=handle"})
    result = await _send(channel)
    assert result == {"outcome": "delivered", "external_message_id": "opaque+/send=handle"}
    call = channel._http.post.await_args
    assert call.args[0].endswith("/v1.0/robot/oToMessages/batchSend")
    assert call.kwargs["json"]["robotCode"] == "robot-test"
    assert call.kwargs["json"]["userIds"] == ["staff-test"]
    assert json.loads(call.kwargs["json"]["msgParam"])["text"] == "test result"
    assert "idempotency_key" not in call.kwargs["json"]


@pytest.mark.parametrize("status,body,outcome", [
    (200, {}, "unknown"),
    (200, {"processQueryKey": "key", "invalidStaffIdList": ["staff-test"]}, "failed"),
    (200, {"processQueryKey": "key", "flowControlledStaffIdList": ["staff-test"]}, "failed"),
    (200, {"processQueryKey": "key", "filteredStaffIdList": ["staff-test"]}, "failed"),
    (200, {"processQueryKey": "key", "invalidStaffIdList": "invalid shape"}, "unknown"),
    (400, {"code": "unknown.send.result"}, "unknown"),
    (400, {"code": "invalidParameter.robotCode.invalid"}, "failed"),
    (400, {}, "unknown"), (429, {}, "failed"), (503, {}, "unknown"),
])
@pytest.mark.asyncio
async def test_platform_receipt_classification_is_conservative(status, body, outcome):
    channel = _channel()
    channel._http.post.return_value = httpx.Response(status, json=body)
    assert (await _send(channel))["outcome"] == outcome
    assert channel._http.post.await_count == 1


@pytest.mark.parametrize("error,outcome", [
    (httpx.ReadTimeout("test timeout"), "unknown"),
    (httpx.ConnectTimeout("test timeout"), "pending"),
    (httpx.ConnectError("test connection refused"), "pending"),
])
@pytest.mark.asyncio
async def test_transport_failure_does_not_retry_an_ambiguous_send(error, outcome):
    channel = _channel()
    channel._http.post.side_effect = error
    assert (await _send(channel))["outcome"] == outcome
    assert channel._http.post.await_count == 1


@pytest.mark.asyncio
async def test_unreachable_token_service_waits_before_asking_again():
    channel = _channel()
    channel._access_token = None
    channel._http.post.side_effect = httpx.ConnectError("test connection refused")
    assert await _send(channel) == {"outcome": "pending", "error_code": "DINGTALK_TOKEN_UNAVAILABLE"}
    assert (await _send(channel))["outcome"] == "pending"
    assert channel._http.post.await_count == 1


@pytest.mark.asyncio
async def test_refused_credentials_fail_instead_of_retrying_every_poll():
    channel = _channel()
    channel._access_token = None
    channel._http.post.return_value = httpx.Response(400, json={"code": "invalidClientIdOrSecret"})
    assert await _send(channel) == {"outcome": "failed", "error_code": "DINGTALK_CREDENTIAL_REJECTED"}
    assert channel._http.post.await_count == 1


@pytest.mark.asyncio
async def test_revoked_cached_token_is_replaced_once_but_a_fresh_one_is_not_retried():
    channel = _channel()
    channel._http.post.return_value = httpx.Response(401, json={})
    assert await _send(channel) == {"outcome": "pending", "error_code": "DINGTALK_TOKEN_EXPIRED"}
    assert channel._access_token is None
    channel._http.post.side_effect = [
        httpx.Response(200, json={"accessToken": "fake-new-token", "expireIn": 7200}),
        httpx.Response(401, json={}),
    ]
    assert (await _send(channel))["outcome"] == "failed"


@pytest.mark.asyncio
async def test_unsupported_file_never_requests_a_token_or_sends():
    channel = _channel()
    channel._access_token = None
    assert (await _send(channel, file_key="file-test"))["outcome"] == "failed"
    channel._http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_claimed_input_precedes_media_download_and_never_falls_back():
    channel = _channel()
    channel.handle_file_message = AsyncMock()
    channel.handle_incoming_message = AsyncMock()
    callback = AsyncMock()
    channel.set_persistent_inbound_callback(callback)
    handler = DingTalkHandler(channel)
    await handler.process(SimpleNamespace(data=_callback(msgtype="file")))
    await handler.process(SimpleNamespace(data=_callback(conversationType="2")))
    channel.handle_file_message.assert_not_awaited()
    channel.handle_incoming_message.assert_not_awaited()
    callback.assert_not_awaited()
    await handler.process(SimpleNamespace(data=_callback()))
    await asyncio.gather(*channel._background_tasks)
    event = callback.await_args.args[0]
    assert event.expected_agent_id == "agent-dingtalk" and event.receiving_app_id == "app-test"
    channel.handle_incoming_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_claimed_route_rejects_whitelist_and_robot_identity_mismatch():
    channel = _channel()
    callback = AsyncMock()
    channel.set_persistent_inbound_callback(callback)
    channel.enqueue_persistent_event(_callback(senderStaffId="not-allowed"))
    channel.enqueue_persistent_event(_callback(robotCode="other-robot"))
    assert not channel._background_tasks
    callback.assert_not_awaited()


def test_routing_keys_keep_the_default_app_on_the_legacy_key():
    router = MagicMock(spec=RobotMessageRouter)
    ordinary = DingTalkChannel(DingTalkConfig(client_id="ordinary"), router)
    converted = DingTalkChannel(DingTalkConfig(client_id="converted", agent_id="agent-a", is_default=True), router)
    other = DingTalkChannel(DingTalkConfig(client_id="other", agent_id="agent-b", is_default=False), router)
    assert ordinary.app_id == converted.app_id == "default" and other.app_id == "other"
    assert converted.persistent_app_id == "converted"
