# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for email subject threading and quote stripping (E6 step 1)."""
from jiuwenswarm.gateway.channel_manager.im_platforms.email.email_format import (
    normalize_subject,
    reply_subject,
    strip_quoted_reply,
    thread_key,
)


def test_normalize_strips_stacked_prefixes():
    assert normalize_subject("Re: Re: Fwd: Deploy v2.3") == "Deploy v2.3"
    assert normalize_subject("RE:  Deploy") == "Deploy"
    assert normalize_subject("Deploy") == "Deploy"


def test_normalize_handles_numbered_and_localized_prefixes():
    assert normalize_subject("Re[2]: Deploy") == "Deploy"
    assert normalize_subject("RÉP: Deploy") == "Deploy"


def test_normalize_of_empty_subject_is_empty():
    assert normalize_subject("   ") == ""
    assert normalize_subject(None) == ""


def test_reply_subject_never_stacks_prefixes():
    assert reply_subject("Re: Deploy") == "Re: Deploy"
    assert reply_subject("Deploy") == "Re: Deploy"
    assert reply_subject("") == "Re:"


def test_thread_key_is_stable_across_reply_prefixes():
    first = thread_key("Alice@Example.com", "Deploy v2.3")
    second = thread_key("alice@example.com", "Re: Deploy v2.3")
    assert first == second


def test_thread_key_separates_senders_and_subjects():
    assert thread_key("a@x.com", "Deploy") != thread_key("b@x.com", "Deploy")
    assert thread_key("a@x.com", "Deploy") != thread_key("a@x.com", "Rollback")


def test_strip_removes_quoted_lines():
    body = "1\n\n> Deploy v2.3?\n> Reply 1: Approve"
    assert strip_quoted_reply(body) == "1"


def test_strip_cuts_at_on_wrote_marker():
    body = "Approve please\n\nOn Tue, Aug 12, 2026 at 10:00 AM, bot <b@x.com> wrote:\nDeploy?"
    assert strip_quoted_reply(body) == "Approve please"


def test_strip_cuts_at_original_message_marker():
    body = "yes\n-----Original Message-----\nfrom bot"
    assert strip_quoted_reply(body) == "yes"


def test_strip_keeps_plain_multiline_answer():
    body = "line one\nline two"
    assert strip_quoted_reply(body) == "line one\nline two"


def test_strip_of_empty_body_is_empty():
    assert strip_quoted_reply("") == ""
    assert strip_quoted_reply(None) == ""