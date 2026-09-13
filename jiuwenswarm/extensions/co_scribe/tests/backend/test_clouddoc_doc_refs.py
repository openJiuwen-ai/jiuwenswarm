"""Document references as the host compares them: exact tokens, trusted origins."""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    doc_ref_token,
    is_trusted_doc_url,
)


@pytest.mark.parametrize("ref,expected", [
    ("1AAAABBBBCCCC", "1AAAABBBBCCCC"),
    ("  1AAAABBBBCCCC ", "1AAAABBBBCCCC"),
    ("https://docs.google.com/document/d/1AAAABBBBCCCC/edit#h", "1AAAABBBBCCCC"),
    ("https://docs.google.com/spreadsheets/d/1AAAABBBBCCCC/edit?gid=0", "1AAAABBBBCCCC"),
    ("https://drive.google.com/open?id=1AAAABBBBCCCC", "1AAAABBBBCCCC"),
    ("https://acme.feishu.cn/docx/AbC123456789?from=x", "AbC123456789"),
    ("https://acme.feishu.cn/wiki/W12345678", "W12345678"),
    ("https://acme.feishu.cn/sheets/S12345678", "S12345678"),
])
def test_doc_ref_token_strips_a_link_to_its_token(ref, expected):
    assert doc_ref_token(ref) == expected


def test_doc_ref_token_compares_by_equality_never_containment():
    assert doc_ref_token("https://docs.google.com/document/d/1AAAABBBBCCCC/edit") != "1AAAABBBBCCC"
    assert doc_ref_token("1AAAABBBBCCCCD") != "1AAAABBBBCCCC"


@pytest.mark.parametrize("url,vendor,expected", [
    ("https://docs.google.com/document/d/x/edit", "google", True),
    ("https://drive.google.com/open?id=x", "google", True),
    ("https://evil.example/document/d/x/edit", "google", False),
    ("https://docs.google.com.evil.example/document/d/x", "google", False),
    ("http://docs.google.com/document/d/x/edit", "google", False),
    ("https://acme.feishu.cn/docx/x", "feishu", True),
    ("https://feishu.cn/docx/x", "feishu", True),
    ("https://acme.larksuite.com/docx/x", "feishu", True),
    ("https://acme.larkoffice.com/docx/x", "feishu", True),
    ("https://acme.feishu.cn.evil.example/docx/x", "feishu", False),
    ("https://evilfeishu.cn/docx/x", "feishu", False),
    ("https://acme.feishu.cn/docx/x", "google", False),
    ("", "feishu", False),
    ("AbC123456789", "feishu", False),
])
def test_is_trusted_doc_url(url, vendor, expected):
    assert is_trusted_doc_url(url, vendor) is expected
