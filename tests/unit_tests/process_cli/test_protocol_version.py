# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.channels.process_cli.protocol.version import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    is_schema_version_supported,
    require_supported_schema_version,
)


def test_one_shot_schema_has_one_experimental_exact_version() -> None:
    assert CURRENT_SCHEMA_VERSION == "0.1"
    assert SUPPORTED_SCHEMA_VERSIONS == (CURRENT_SCHEMA_VERSION,)
    assert len(set(SUPPORTED_SCHEMA_VERSIONS)) == len(SUPPORTED_SCHEMA_VERSIONS)
    assert is_schema_version_supported("0.1") is True


@pytest.mark.parametrize("version", ["0.2", "1.0", "v0.1", " 0.1 ", 0.1, None])
def test_unknown_or_malformed_schema_versions_are_rejected(version: object) -> None:
    assert is_schema_version_supported(version) is False
    with pytest.raises((TypeError, ValueError)):
        require_supported_schema_version(version)


def test_one_shot_schema_does_not_expose_connection_negotiation() -> None:
    import jiuwenswarm.channels.process_cli.protocol.version as version

    assert not hasattr(version, "negotiate_protocol_version")
