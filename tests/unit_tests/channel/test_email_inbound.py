# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for inbound e-mail parsing (E6 step 3)."""
from email.message import EmailMessage

from jiuwenswarm.gateway.channel_manager.im_platforms.email.email_inbound import (
    decode_field,
    is_from_self,
    parse_inbound,
    plain_text_body,
    sender_address,
    subject_of,
)


def _mail(body="hello", sender="Alice <Alice@X.com>", subject="Deploy v2.3"):
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "bot@x.com"
    mail["Subject"] = subject
    mail.set_content(body)
    return mail


def test_sender_address_is_bare_and_lowercased():
    assert sender_address(_mail()) == "alice@x.com"


def test_subject_is_decoded():
    assert subject_of(_mail(subject="Deploy v2.3")) == "Deploy v2.3"


def test_decode_field_handles_encoded_words():
    assert decode_field("=?utf-8?q?D=C3=A9ploiement?=") == "Déploiement"


def test_decode_field_of_empty_is_empty():
    assert decode_field("") == ""


def test_plain_text_body_of_simple_mail():
    assert plain_text_body(_mail("hello there")).strip() == "hello there"


def test_plain_text_body_prefers_text_plain_part():
    mail = _mail("plain version")
    mail.add_alternative("<p>html version</p>", subtype="html")
    assert "plain version" in plain_text_body(mail)


def test_parse_inbound_returns_sender_session_and_clean_content():
    mail = _mail("1\n\n> Deploy v2.3?\n> Reply 1: Approve")
    parsed = parse_inbound(mail)
    assert parsed["sender"] == "alice@x.com"
    assert parsed["content"] == "1"
    assert parsed["session_id"] == "alice@x.com|deploy v2.3"


def test_parse_inbound_session_is_stable_across_reply_prefixes():
    first = parse_inbound(_mail("ok", subject="Deploy v2.3"))
    second = parse_inbound(_mail("ok", subject="Re: Deploy v2.3"))
    assert first["session_id"] == second["session_id"]


def test_parse_inbound_returns_none_without_sender():
    mail = EmailMessage()
    mail["Subject"] = "Deploy"
    mail.set_content("hello")
    assert parse_inbound(mail) is None


def test_parse_inbound_returns_none_when_only_quoted_text():
    assert parse_inbound(_mail("> Deploy v2.3?\n> Reply 1: Approve")) is None


def test_parse_inbound_returns_none_on_empty_body():
    assert parse_inbound(_mail("   ")) is None


def test_is_from_self_detects_the_bot_own_address():
    assert is_from_self(_mail(sender="bot@x.com"), "Bot@X.com") is True
    assert is_from_self(_mail(), "bot@x.com") is False


def test_is_from_self_is_false_without_own_address():
    assert is_from_self(_mail(), "") is False