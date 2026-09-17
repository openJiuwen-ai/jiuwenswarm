# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity


@pytest.mark.parametrize(
    "kind,version",
    [("skill", "1.0.0"), ("agent_template", "abc1234"),
     ("plugin", "1.0.0"), ("mcp", "abc1234")],
)
def test_supported_identity(kind, version):
    assert PublishIdentity(kind, "sales-assistant", version).version == version


@pytest.mark.parametrize("version", ["v1.0.0", "1.0", "ABC1234", ""])
def test_reject_invalid_version(version):
    with pytest.raises(ValueError):
        PublishIdentity("plugin", "sales-assistant", version)


def test_agent_group_is_not_a_publish_kind():
    with pytest.raises(ValueError):
        PublishIdentity("agent_group", "sales-assistant", "1.0.0")


@pytest.mark.parametrize("name", ["", " sales-assistant", "sales-assistant "])
def test_no_silent_name_normalization(name):
    with pytest.raises(ValueError):
        PublishIdentity("plugin", name, "1.0.0")


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", None),
        ("kind", []),
        ("package_name", 12),
        ("version", None),
        ("target_asset_id", 12),
        ("target_asset_id", " asset-1"),
    ],
)
def test_malformed_inputs_have_safe_validation_errors(field, value):
    values = dict(kind="plugin", package_name="sales-assistant", version="1.0.0")
    values[field] = value
    with pytest.raises(ValueError):
        PublishIdentity(**values)
