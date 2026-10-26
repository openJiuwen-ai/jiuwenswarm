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
    assert both["assistant_secret"] == "hidden"

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

    with patch("jiuwenswarm.common.config.update_channel_in_config") as update:
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
    assert update.call_args.args[0] == "welink"
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
    assert stored["assistant_credential_id"] == "2026"
    public = public_welink_authorization(stored)
    assert public["assistant_ready"] is True
    assert "hidden" not in str(public)


def test_switching_colleague_drops_the_previous_half():
    merged = apply_welink_authorization(
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
    assert merged["assistant_secret"] == ""
    assert merged["cli_authorized"] is False
    assert merged["enabled"] is False
