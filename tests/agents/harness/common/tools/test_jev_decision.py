# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Jev client: wire format, retries, errors, replay cache, routing. Offline."""

import json

import pytest

from jiuwenswarm.agents.harness.common.tools import jev_decision as jev

RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {"relevant": {"type": "noul", "noul": 0.66}},
    "usage": {"input_tokens": 416, "output_tokens": 60},
}


def _post(replies, sent):
    def post(url, body, headers, timeout):
        sent.append((url, json.loads(body), headers))
        return replies.pop(0)
    return post


def test_request_follows_the_official_wire_format():
    sent = []
    client = jev.JevClient("k", post=_post([(200, json.dumps(RESPONSE))], sent))
    out = client.decide("state", {"relevant": jev.noul("Is it relevant?")})
    url, body, headers = sent[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert headers["Authorization"] == "Bearer k"
    assert body == {"model": "jev-latest", "state": "state",
                    "questions": {"relevant": {"type": "noul", "instructions": "Is it relevant?"}}}
    assert out["answers"]["relevant"]["noul"] == 0.66


def test_rate_limit_is_retried_with_backoff():
    sent, waits = [], []
    client = jev.JevClient("k", post=_post([(429, ""), (529, ""), (200, json.dumps(RESPONSE))], sent),
                           sleep=waits.append)
    client.decide("s", {"q": jev.noul("?")})
    assert len(sent) == 3 and waits == [1, 2]


def test_bad_key_and_invalid_body_are_errors_not_retries():
    for status in (401, 422):
        sent = []
        client = jev.JevClient("k", post=_post([(status, "bad field")], sent))
        with pytest.raises(jev.JevError):
            client.decide("s", {"q": jev.noul("?")})
        assert len(sent) == 1


def test_replay_answers_from_the_cache_and_sends_nothing(tmp_path):
    cache = tmp_path / "jev_cache.jsonl"
    sent = []
    live = jev.JevClient("k", cache_path=cache, post=_post([(200, json.dumps(RESPONSE))], sent))
    live.decide("s", {"q": jev.noul("?")})
    replay = jev.JevClient(cache_path=cache, replay=True,
                           post=lambda *a: pytest.fail("replay must not send"))
    assert replay.decide("s", {"q": jev.noul("?")}) == RESPONSE
    with pytest.raises(jev.JevError):
        replay.decide("another state", {"q": jev.noul("?")})


def test_usage_row_uses_jiuwenswarm_token_keys_and_bills_input_only():
    row = jev.usage_row(RESPONSE, stage="analyze", step="relevance_screen", source_record="x")
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (416, 60, 476)
    assert row["cost_usd"] == pytest.approx(416 * 0.042 / 1e6)
    assert row["source_runtime"] == "jiuwenswarm"


def test_decisions_route_to_jev_and_writing_does_not():
    assert jev.route_for("relevance_screen") == "jev"
    assert jev.route_for("design_checklist") == "jev"
    assert jev.route_for("section_draft") == "chat"


def test_choice_needs_two_options():
    with pytest.raises(ValueError):
        jev.choice("?", {"only": None})
