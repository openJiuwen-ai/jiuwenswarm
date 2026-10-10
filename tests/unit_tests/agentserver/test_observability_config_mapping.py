# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.agents.harness import observability_runtime
from jiuwenswarm.agents.harness.observability_runtime import build_observability_config


@pytest.mark.parametrize("service_name", ["jiuwenswarm", "jiuwenswarm-agent"])
def test_team_and_agent_callers_keep_the_configured_exporter(service_name: str) -> None:
    # Team and single-agent runtimes call the builder the same way; neither
    # may end up with a different transport than the yaml selected.
    config = build_observability_config(
        {"exporter": "file"},
        service_name=service_name,
        traces_dir="/tmp/traces",
    )

    assert config.exporter == "file"
    assert config.traces_dir == "/tmp/traces"
    assert config.max_attributes == 200


def test_attribute_limit_can_be_configured() -> None:
    config = build_observability_config(
        {"max_attributes": 512},
        service_name="jiuwenswarm-agent",
        traces_dir="/tmp/traces",
    )

    assert config.max_attributes == 512


def test_yaml_backend_key_is_ignored_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        observability_runtime.logger,
        "warning",
        lambda message, *args: warnings.append(message % args if args else message),
    )

    config = build_observability_config(
        {"backend": "langfuse", "exporter": "file"},
        service_name="jiuwenswarm",
        traces_dir="/tmp/traces",
    )

    assert config.exporter == "file"
    assert not hasattr(config, "backend")
    assert any("'backend' has been removed" in message for message in warnings)
