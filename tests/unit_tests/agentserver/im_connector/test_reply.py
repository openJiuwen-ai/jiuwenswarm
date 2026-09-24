from __future__ import annotations

from jiuwenswarm.server.im.im_connector.reply import format_outgoing_reply_text


def test_format_outgoing_reply_text_is_channel_agnostic():
    assert format_outgoing_reply_text(
        "你好",
        account="w3alice",
        mention_sender=True,
        sender="bob",
        max_reply_chars=1000,
    ) == "@bob 来自 w3alice 的数字分身：你好"
    assert format_outgoing_reply_text(
        "你好",
        account="DGUself",
        mention_sender=True,
        sender="DGUother",
        mention_token="<@DGUother> ",
    ) == "<@DGUother> 来自 DGUself 的数字分身：你好"
