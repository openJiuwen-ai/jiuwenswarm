# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.agents.harness.observability_runtime import build_observability_config


def test_single_agent_native_backend_defaults_are_forwarded() -> None:
    config = build_observability_config(
        {},
        service_name="jiuwenswarm-agent",
        default_backend="otlp",
        traces_dir="/tmp/traces",
    )

    assert config.backend == "otlp"
    assert config.max_attributes == 200


def test_observability_backend_and_attribute_limit_can_be_configured() -> None:
    config = build_observability_config(
        {"backend": "langfuse", "max_attributes": 512},
        service_name="jiuwenswarm-agent",
        default_backend="otlp",
        traces_dir="/tmp/traces",
    )

    assert config.backend == "langfuse"
    assert config.max_attributes == 512
