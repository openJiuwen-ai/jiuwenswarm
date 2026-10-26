# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""IdentityInfo 数据结构测试。"""
from jiuwenswarm.extensions.identity_provider.types import IdentityInfo


def test_identity_info_default_fields():
    info = IdentityInfo()
    assert info.user_id is None
    assert info.domain_id is None
    assert info.app_id is None
    assert info.extra == {}


def test_identity_info_with_fields():
    info = IdentityInfo(user_id="u1", domain_id="d1", app_id="a1")
    assert info.user_id == "u1"
    assert info.domain_id == "d1"
    assert info.app_id == "a1"


def test_to_dict_with_all_fields():
    info = IdentityInfo(user_id="u1", domain_id="d1", app_id="a1", extra={"k": "v"})
    assert info.to_dict() == {"user_id": "u1", "domain_id": "d1", "app_id": "a1", "extra": {"k": "v"}}


def test_to_dict_with_partial_fields():
    info = IdentityInfo(user_id="u1", domain_id=None, app_id="a1")
    result = info.to_dict()
    assert "user_id" in result and "domain_id" not in result
    assert result["user_id"] == "u1" and result["app_id"] == "a1"


def test_to_dict_all_none():
    assert IdentityInfo().to_dict() == {}
