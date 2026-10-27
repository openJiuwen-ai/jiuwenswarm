# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Assistant QR login and the two-step colleague binding."""

from __future__ import annotations

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_assistant_login import (
    AssistantLoginManager,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_binding import (
    apply_welink_authorization,
    public_welink_authorization,
    reveal_new_assistant_credential,
)


def _fetch(script):
    calls = {"n": 0}

    def fetch(method, url, headers, body):
        item = script[calls["n"]]
        calls["n"] += 1
        return item

    return fetch


def test_assistant_qr_then_confirm_hides_the_secret():
    secret = "secret-value"
    script = [
        (200, {"code": "200", "data": {"qrcode": "qr-1", "accessToken": "access-1", "weUrl": "welink://assistant"}}),
        (200, {"code": 200, "data": {"status": 0}}),
        (200, {"code": "200", "data": {"status": 2, "ak": "202610220000", "sk": secret}}),
    ]
    manager = AssistantLoginManager(fetch=_fetch(script))
    started = manager.start(agent_id="mate_1", account="p_openjiuwenbot1")
    assert started.status == "pending"
    assert started.snapshot()["qr_payload"] == "welink://assistant"
    assert "access-1" not in str(started.snapshot())
    again = manager.start(agent_id="mate_1", account="p_openjiuwenbot1")
    assert again.session_id == started.session_id
    waiting = manager.refresh(started)
    assert waiting["status"] == "pending"
    confirmed = manager.refresh(started)
    assert confirmed["status"] == "success"
    assert secret not in str(confirmed)
    assert "sk" not in confirmed
    credential_id, stored_secret = started.credentials()
    assert credential_id == "202610220000"
    assert stored_secret == secret


def test_a_new_assistant_scan_is_shown_once_and_stays_out_of_later_status():
    secret = "secret-value"
    shown = reveal_new_assistant_credential("202610220000", secret)
    assert shown["assistant_credential_id"] == "202610220000"
    assert shown["assistant_secret"] == secret
    assert reveal_new_assistant_credential("", secret) == {}
    public = public_welink_authorization(
        {
            "persistent_agent_id": "mate_1",
            "account": "h00557917",
            "assistant_credential_id": "202610220000",
            "assistant_secret": secret,
        }
    )
    assert "assistant_secret" not in public
    assert secret not in str(public)


def test_either_authorization_order_stays_disabled_until_both_exist():
    assistant_first = apply_welink_authorization(
        {},
        {
            "persistent_agent_id": "mate_1",
            "account": "h00557917",
            "assistant_credential_id": "2026",
            "assistant_secret": "hidden",
        },
    )
    assert assistant_first["enabled"] is False
    both = apply_welink_authorization(
        assistant_first,
        {
            "persistent_agent_id": "mate_1",
            "account": "h00557917",
            "credential_dir": "C:/welink/mate_1",
            "cli_authorized": True,
        },
    )
    assert both["enabled"] is True
    assert both["bindings"][0]["assistant_secret"] == "hidden"

    cli_first = apply_welink_authorization(
        {},
        {
            "persistent_agent_id": "mate_1",
            "account": "h00557917",
            "credential_dir": "C:/welink/mate_1",
            "cli_authorized": True,
        },
    )
    assert cli_first["enabled"] is False
    public = public_welink_authorization(both)
    assert public["assistant_ready"] is True
    assert public["cli_ready"] is True
    assert "assistant_secret" not in public
    assert "hidden" not in str(public)


def test_cancelled_expired_and_incomplete_qr_do_not_store_a_secret():
    from unittest.mock import patch

    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_binding import (
        save_welink_authorization,
    )

    cancelled = AssistantLoginManager(fetch=_fetch([
        (200, {"code": 200, "data": {"qrcode": "qr", "accessToken": "access", "weUrl": "h5://assistant"}}),
        (200, {"code": "200", "data": {"status": 3}}),
    ]))
    session = cancelled.start(agent_id="mate_1", account="h00557917")
    assert cancelled.refresh(session)["error"] == "已取消助手授权"
    assert session.credentials() == ("", "")

    expired = AssistantLoginManager(fetch=_fetch([
        (200, {"code": 200, "data": {"qrcode": "qr", "accessToken": "access", "weUrl": "h5://assistant"}}),
        (200, {"code": 200, "data": {"status": 2, "expired": "true", "ak": "should-not-store", "sk": "hidden"}}),
    ]))
    expired_session = expired.start(agent_id="mate_1", account="h00557917")
    result = expired.refresh(expired_session)
    assert result["status"] == "failed"
    assert "hidden" not in str(result)
    assert expired_session.credentials() == ("", "")

    incomplete = AssistantLoginManager(fetch=_fetch([
        (200, {"code": 200, "data": {"qrcode": "qr"}}),
    ]))
    failed = incomplete.start(agent_id="mate_1", account="h00557917")
    assert failed.status == "failed"
    assert failed.credentials() == ("", "")

    with patch(
        "jiuwenswarm.common.config.update_config",
        side_effect=lambda mutate: mutate({"channels": {}}),
    ) as update:
        view, stored = save_welink_authorization(
            {
                "persistent_agent_id": "mate_1",
                "account": "h00557917",
                "assistant_credential_id": "2026",
                "assistant_secret": "hidden",
            },
            current={},
        )
    update.assert_called_once()
    assert stored["enabled"] is False
    assert "assistant_secret" not in view
    assert "hidden" not in str(view)


def test_typed_assistant_credential_stays_disabled_until_cli_and_hides_the_secret():
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_binding import (
        manual_assistant_patch,
    )

    try:
        manual_assistant_patch(agent_id="mate_1", account="h00557917", credential_id="2026", secret="  ")
    except ValueError:
        rejected = True
    else:
        rejected = False
    assert rejected

    patch = manual_assistant_patch(
        agent_id=" mate_1 ",
        account=" h00557917 ",
        credential_id=" 2026 ",
        secret=" hidden ",
    )
    stored = apply_welink_authorization({}, patch)
    assert stored["enabled"] is False
    assert stored["bindings"][0]["assistant_credential_id"] == "2026"
    public = public_welink_authorization(stored)
    assert public["assistant_ready"] is True
    assert "hidden" not in str(public)


def test_another_reader_account_is_rejected_without_dropping_existing_binding():
    import pytest

    with pytest.raises(ValueError, match="shared reader account"):
        apply_welink_authorization(
            {
                "persistent_agent_id": "mate_old",
                "assistant_credential_id": "old-id",
                "assistant_secret": "old-secret",
                "cli_authorized": True,
                "credential_dir": "C:/old",
                "account": "h00557917",
            },
            {"persistent_agent_id": "mate_new", "account": "p_openjiuwenbot1"},
        )


def test_welink_save_keeps_another_channel_write_when_they_overlap(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from jiuwenswarm.common import config
    from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_binding import save_welink_authorization

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    config.dump_yaml_round_trip(path, {"channels": {"dingtalk": {"apps": []}}})
    original_dump = config.dump_yaml_round_trip
    welink_writing, release, other_started, other_writing = (threading.Event() for _ in range(4))

    def dump(file, data):
        channels = data["channels"]
        if channels.get("welink", {}).get("bindings") and not welink_writing.is_set():
            welink_writing.set()
            assert release.wait(timeout=3)
        elif channels["dingtalk"].get("apps"):
            other_writing.set()
        return original_dump(file, data)

    def other_write():
        other_started.set()

        def update(data):
            data["channels"]["dingtalk"]["apps"] = [{"client_id": "other-app"}]
            return data

        return config.update_config(update)

    monkeypatch.setattr(config, "dump_yaml_round_trip", dump)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save_welink_authorization, {
            "persistent_agent_id": "welink-agent", "account": "account-a",
            "assistant_credential_id": "fake-id", "assistant_secret": "fake",
        })
        assert welink_writing.wait(timeout=3)
        second = pool.submit(other_write)
        try:
            assert other_started.wait(timeout=3)
            assert not other_writing.wait(timeout=0.1)
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
    stored = config.load_yaml_round_trip(path)["channels"]
    assert stored["welink"]["bindings"][0]["persistent_agent_id"] == "welink-agent"
    assert stored["dingtalk"]["apps"] == [{"client_id": "other-app"}]
