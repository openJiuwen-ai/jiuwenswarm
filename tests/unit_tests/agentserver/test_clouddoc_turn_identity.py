"""The agentserver's two identity helpers for a co-scribe turn: which credentials a
turn runs under, and what address they act as."""

from __future__ import annotations

import json

import pytest


def _helpers():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _clouddoc_credentials_for_turn,
        _clouddoc_self_address,
    )

    return _clouddoc_credentials_for_turn, _clouddoc_self_address


SPECS = [
    {"credentials_file": "/k/first.json", "documents": ["1AAAABBBBCCCC"]},
    {"credentials_file": "/k/second.json", "documents": [
        "1AAAABBBBCCCCD", "https://acme.feishu.cn/docx/FsTok1?from=x",
    ]},
]


@pytest.mark.parametrize("turn_doc,expected", [
    ("1AAAABBBBCCCC", "/k/first.json"),
    ("1AAAABBBBCCCCD", "/k/second.json"),      # one character longer: the other connection
    ("1AAAABBBBCCC", "/k/first.json"),         # a prefix of a listed id: nobody's; the default
    ("FsTok1", "/k/second.json"),              # listed as a link, compared as its token
    ("https://acme.feishu.cn/docx/FsTok1?from=x", "/k/second.json"),   # dispatched as the link itself
    ("https://acme.feishu.cn/docx/FsTok1", "/k/second.json"),
    ("https://docs.google.com/document/d/1AAAABBBBCCCC/edit", "/k/first.json"),
    ("FsTok10", "/k/first.json"),
    (None, "/k/first.json"),
    ("", "/k/first.json"),
])
def test_credentials_follow_the_exact_document_id(turn_doc, expected):
    credentials_for_turn, _ = _helpers()
    assert credentials_for_turn(SPECS, turn_doc) == expected


def test_self_address_reads_either_vendors_key(tmp_path):
    _, self_address = _helpers()
    g = tmp_path / "g.json"
    g.write_text(json.dumps({"type": "service_account", "client_email": "sa@x.iam"}))
    f = tmp_path / "f.json"
    f.write_text(json.dumps({"app_id": "cli_x", "app_secret": "s", "bot_open_id": "ou_bot"}))
    assert self_address(str(g)) == "sa@x.iam"
    assert self_address(str(f)) == "ou_bot"
    assert self_address(str(tmp_path / "nope.json")) == ""
