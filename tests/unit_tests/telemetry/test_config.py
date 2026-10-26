from dataclasses import asdict
from typing import get_type_hints

import pytest

from jiuwenswarm.telemetry import attributes
from jiuwenswarm.telemetry.config import TelemetryConfig, load_telemetry_config


_OTEL_ENV_KEYS = (
    "OTEL_ENABLED",
    "OTEL_EXPORTER_TYPE",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_SERVICE_NAME",
    "OTEL_LOG_MESSAGES",
    "OTEL_CLAW_ID",
    "OTEL_TRACES_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "OTEL_SAMPLE_RATE",
    "OTEL_MAX_ATTRIBUTES",
    "OTEL_ATTRIBUTE_VALUE_MAX_LENGTH",
    "OTEL_REDACT_PROMPTS",
    "OTEL_REDACT_COMPLETIONS",
    "OTEL_SESSION_STUCK_THRESHOLD_MS",
    "OTEL_SESSION_STUCK_CHECK_INTERVAL_S",
    "OTEL_ENTERPRISE_RAIL",
)


@pytest.fixture(autouse=True)
def clear_otel_environment(monkeypatch):
    for key in _OTEL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_signal_env_overrides_yaml(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "enabled": True,
                "endpoint": "http://yaml:4317",
                "traces": {"endpoint": "http://yaml-traces:4318"},
            }
        },
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://env:4318")

    cfg = load_telemetry_config()

    assert cfg.enabled is True
    assert cfg.traces_endpoint == "http://env:4318"


def test_absent_telemetry_keeps_legacy_mode(monkeypatch):
    monkeypatch.setattr("jiuwenswarm.telemetry.config.get_config", lambda: {})

    cfg = load_telemetry_config()

    assert cfg.enabled is False
    assert cfg.unified_mode is False


def test_telemetry_config_defaults_are_exact_and_frozen(monkeypatch):
    cfg = TelemetryConfig()

    assert asdict(cfg) == {
        "enabled": False,
        "exporter": "none",
        "endpoint": "http://localhost:4317",
        "protocol": "grpc",
        "headers": {},
        "traces_exporter": "none",
        "traces_endpoint": "http://localhost:4317",
        "traces_protocol": "grpc",
        "traces_headers": {},
        "metrics_exporter": "none",
        "metrics_endpoint": "http://localhost:4317",
        "metrics_protocol": "grpc",
        "metrics_headers": {},
        "logs_exporter": "none",
        "logs_endpoint": "http://localhost:4317",
        "logs_protocol": "grpc",
        "logs_headers": {},
        "service_name": "jiuwenclaw",
        "sample_rate": 1.0,
        "max_attributes": 128,
        "attribute_value_max_length": 10240,
        "redact_prompts": False,
        "redact_completions": False,
        "log_messages": True,
        "claw_id": None,
        "session_stuck_threshold_ms": 300000.0,
        "session_stuck_check_interval_s": 30.0,
    }
    assert cfg.unified_mode is False
    with pytest.raises(AttributeError):
        cfg.enabled = True

    monkeypatch.setattr("jiuwenswarm.telemetry.config.get_config", lambda: {})
    assert load_telemetry_config().service_name == "jiuwenclaw"


def test_telemetry_headers_preserve_exact_dict_contract_and_independent_defaults():
    hints = get_type_hints(TelemetryConfig)
    assert hints["headers"] == dict[str, str]
    assert hints["traces_headers"] == dict[str, str]
    assert hints["metrics_headers"] == dict[str, str]

    cfg = TelemetryConfig(
        headers={"common": "one"},
        traces_headers={"trace": "two"},
        metrics_headers={"metric": "three"},
    )

    for headers in (cfg.headers, cfg.traces_headers, cfg.metrics_headers):
        assert type(headers) is dict
    cfg.headers["grpc-metadata"] = "accepted"
    assert cfg.headers == {"common": "one", "grpc-metadata": "accepted"}

    first = TelemetryConfig()
    second = TelemetryConfig()
    first.headers["only-first"] = "value"
    assert second.headers == {}


def test_nested_and_flat_signal_yaml_take_precedence_over_common_yaml(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "exporter": "console",
                "endpoint": "http://common:4317",
                "protocol": "http",
                "headers": {"common": "header"},
                "traces": {
                    "exporter": "otlp",
                    "endpoint": "http://trace:4318",
                    "protocol": "grpc",
                    "headers": "trace=yes",
                },
                "metrics_exporter": "console",
                "metrics_endpoint": "http://metrics:4319",
                "metrics_protocol": "http",
                "metrics_headers": {"metric": "yes"},
            }
        },
    )

    cfg = load_telemetry_config()

    assert (cfg.traces_exporter, cfg.traces_endpoint, cfg.traces_protocol) == (
        "otlp",
        "http://trace:4318",
        "grpc",
    )
    assert cfg.traces_headers == {"trace": "yes"}
    assert (cfg.metrics_exporter, cfg.metrics_endpoint, cfg.metrics_protocol) == (
        "console",
        "http://metrics:4319",
        "http",
    )
    assert cfg.metrics_headers == {"metric": "yes"}


def test_common_and_signal_env_precedence_and_empty_values_fall_back(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "exporter": "yaml-exporter",
                "endpoint": "http://yaml-common:4317",
                "protocol": "http",
                "headers": "yaml=common",
                "traces": {"endpoint": "http://yaml-trace:4318"},
            }
        },
    )
    monkeypatch.setenv("OTEL_EXPORTER_TYPE", "env-exporter")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env-common:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "env-metrics")

    cfg = load_telemetry_config()

    assert cfg.exporter == "env-exporter"
    assert cfg.endpoint == "http://env-common:4317"
    assert cfg.traces_endpoint == "http://yaml-trace:4318"
    assert cfg.metrics_exporter == "env-metrics"
    assert cfg.metrics_endpoint == "http://env-common:4317"


def test_headers_support_dict_and_csv_and_ignore_invalid_keys(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "headers": {" yaml ": " common ", "": "dropped"},
                "traces_headers": "trace=yaml,invalid,=dropped",
            }
        },
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "one=1, invalid, =no, two = 2")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_HEADERS", "metric=yes")

    cfg = load_telemetry_config()

    assert cfg.headers == {"one": "1", "two": "2"}
    assert cfg.traces_headers == {"trace": "yaml"}
    assert cfg.metrics_headers == {"metric": "yes"}


def test_invalid_coercions_use_yaml_or_defaults_and_bounds_are_applied(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "enabled": "yes",
                "sample_rate": "0.25",
                "max_attributes": "10",
                "attribute_value_max_length": "20",
                "redact_prompts": "no",
                "redact_completions": "yes",
                "log_messages": "no",
                "session": {"stuck_threshold_ms": "5", "stuck_check_interval_s": "6"},
            }
        },
    )
    monkeypatch.setenv("OTEL_ENABLED", "invalid")
    monkeypatch.setenv("OTEL_SAMPLE_RATE", "bad")
    monkeypatch.setenv("OTEL_MAX_ATTRIBUTES", "bad")
    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_MAX_LENGTH", "0")
    monkeypatch.setenv("OTEL_REDACT_PROMPTS", "invalid")
    monkeypatch.setenv("OTEL_REDACT_COMPLETIONS", "false")
    monkeypatch.setenv("OTEL_LOG_MESSAGES", "invalid")
    monkeypatch.setenv("OTEL_SESSION_STUCK_THRESHOLD_MS", "-3")
    monkeypatch.setenv("OTEL_SESSION_STUCK_CHECK_INTERVAL_S", "bad")

    cfg = load_telemetry_config()

    assert cfg.enabled is True
    assert cfg.sample_rate == 0.25
    assert cfg.max_attributes == 10
    assert cfg.attribute_value_max_length == 1
    assert cfg.redact_prompts is False
    assert cfg.redact_completions is False
    assert cfg.log_messages is False
    assert cfg.session_stuck_threshold_ms == 0.0
    assert cfg.session_stuck_check_interval_s == 6.0


def test_custom_env_values_are_coerced_and_clamped(monkeypatch):
    monkeypatch.setattr("jiuwenswarm.telemetry.config.get_config", lambda: {"telemetry": {}})
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_SAMPLE_RATE", "9")
    monkeypatch.setenv("OTEL_MAX_ATTRIBUTES", "-1")
    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_MAX_LENGTH", "3")
    monkeypatch.setenv("OTEL_REDACT_PROMPTS", "yes")
    monkeypatch.setenv("OTEL_REDACT_COMPLETIONS", "0")
    monkeypatch.setenv("OTEL_LOG_MESSAGES", "false")
    monkeypatch.setenv("OTEL_SESSION_STUCK_THRESHOLD_MS", "4.5")
    monkeypatch.setenv("OTEL_SESSION_STUCK_CHECK_INTERVAL_S", "2")
    monkeypatch.setenv("OTEL_CLAW_ID", "  claw-1  ")

    cfg = load_telemetry_config()

    assert cfg.enabled is True
    assert cfg.unified_mode is True
    assert cfg.sample_rate == 1.0
    assert cfg.max_attributes == 1
    assert cfg.attribute_value_max_length == 3
    assert cfg.redact_prompts is True
    assert cfg.redact_completions is False
    assert cfg.log_messages is False
    assert cfg.session_stuck_threshold_ms == 4.5
    assert cfg.session_stuck_check_interval_s == 2.0
    assert cfg.claw_id == "claw-1"


def test_invalid_yaml_and_enterprise_rail_are_safely_ignored(monkeypatch):
    monkeypatch.setattr("jiuwenswarm.telemetry.config.get_config", lambda: {"telemetry": "bad"})
    monkeypatch.setenv("OTEL_ENTERPRISE_RAIL", "true")

    cfg = load_telemetry_config()

    assert cfg == TelemetryConfig()
    assert not hasattr(cfg, "enterprise_rail")


def test_config_loader_failure_and_non_finite_numbers_fall_back_safely(monkeypatch):
    def fail_to_load():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("jiuwenswarm.telemetry.config.get_config", fail_to_load)
    monkeypatch.setenv("OTEL_SAMPLE_RATE", "nan")
    monkeypatch.setenv("OTEL_SESSION_STUCK_THRESHOLD_MS", "inf")

    cfg = load_telemetry_config()

    assert cfg.sample_rate == 1.0
    assert cfg.session_stuck_threshold_ms == 300000.0


def test_yaml_numeric_booleans_are_supported_without_accepting_other_numbers(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "enabled": 1,
                "redact_prompts": 0,
                "redact_completions": 2,
                "log_messages": -1,
            }
        },
    )

    cfg = load_telemetry_config()

    assert cfg.enabled is True
    assert cfg.redact_prompts is False
    assert cfg.redact_completions is False
    assert cfg.log_messages is True


def test_overflowing_numeric_values_fall_back_without_raising(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "sample_rate": 10**10000,
                "max_attributes": float("inf"),
                "attribute_value_max_length": float("inf"),
            }
        },
    )

    cfg = load_telemetry_config()

    assert cfg.sample_rate == 1.0
    assert cfg.max_attributes == 128
    assert cfg.attribute_value_max_length == 10240


def test_invalid_common_header_env_falls_back_to_yaml(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {"telemetry": {"headers": "yaml=yes"}},
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "not-a-header,=nope")

    assert load_telemetry_config().headers == {"yaml": "yes"}


def test_invalid_signal_header_env_falls_back_to_signal_yaml_then_common(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "headers": "common=yes",
                "traces": {"headers": "signal=yes"},
            }
        },
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "invalid")

    assert load_telemetry_config().traces_headers == {"signal": "yes"}

    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {"telemetry": {"headers": "common=yes", "traces": {"headers": {"": "x"}}}},
    )

    assert load_telemetry_config().traces_headers == {"common": "yes"}


def test_empty_signal_header_placeholders_fall_back_to_common_auth_headers(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "headers": "Authorization=Bearer shared",
                "traces": {"headers": {}},
                "metrics": {"headers": {}},
            }
        },
    )

    cfg = load_telemetry_config()

    assert cfg.traces_headers == {"Authorization": "Bearer shared"}
    assert cfg.metrics_headers == {"Authorization": "Bearer shared"}


def test_blank_or_invalid_flat_signal_headers_fall_back_to_nested_yaml(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "traces_headers": "",
                "metrics_headers": "not-a-header",
                "traces": {"headers": "trace=yes"},
                "metrics": {"headers": "metric=yes"},
            }
        },
    )

    cfg = load_telemetry_config()

    assert cfg.traces_headers == {"trace": "yes"}
    assert cfg.metrics_headers == {"metric": "yes"}


def test_blank_flat_signal_value_falls_back_to_nested_yaml(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "traces_endpoint": "",
                "traces": {"endpoint": "http://nested:4318"},
            }
        },
    )

    assert load_telemetry_config().traces_endpoint == "http://nested:4318"


def test_integer_fields_reject_bools_and_non_integral_floats(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.config.get_config",
        lambda: {
            "telemetry": {
                "max_attributes": True,
                "attribute_value_max_length": 1.9,
            }
        },
    )

    cfg = load_telemetry_config()

    assert cfg.max_attributes == 128
    assert cfg.attribute_value_max_length == 10240


def test_attribute_primary_keys_and_legacy_aliases_are_stable():
    assert attributes.USER_ID == "user.id"
    assert attributes.DOMAIN_ID == "domain.id"
    assert attributes.APP_ID == "app.id"
    assert {
        "claw_id": attributes.JIUWENCLAW_CLAW_ID,
        "channel_id": attributes.JIUWENCLAW_CHANNEL_ID,
        "session_id": attributes.JIUWENCLAW_SESSION_ID,
        "user_id": attributes.JIUWENCLAW_USER_ID,
        "domain_id": attributes.JIUWENCLAW_DOMAIN_ID,
        "app_id": attributes.JIUWENCLAW_APP_ID,
        "request_id": attributes.JIUWENCLAW_REQUEST_ID,
        "agent_name": attributes.JIUWENCLAW_AGENT_NAME,
        "session_state": attributes.JIUWENCLAW_SESSION_STATE,
        "session_state_reason": attributes.JIUWENCLAW_SESSION_STATE_REASON,
        "iteration": attributes.JIUWENCLAW_ITERATION,
        "agent_parent": attributes.JIUWENCLAW_AGENT_PARENT,
        "canceled": attributes.JIUWENCLAW_CANCELED,
    } == {
        "claw_id": "jiuwenclaw.claw.id",
        "channel_id": "jiuwenclaw.channel.id",
        "session_id": "jiuwenclaw.session.id",
        "user_id": "jiuwenclaw.user.id",
        "domain_id": "jiuwenclaw.domain.id",
        "app_id": "jiuwenclaw.app.id",
        "request_id": "jiuwenclaw.request.id",
        "agent_name": "jiuwenclaw.agent.name",
        "session_state": "jiuwenclaw.session.state",
        "session_state_reason": "jiuwenclaw.session.state.reason",
        "iteration": "jiuwenclaw.iteration",
        "agent_parent": "jiuwenclaw.agent.parent",
        "canceled": "jiuwenclaw.canceled",
    }
    assert attributes.GEN_AI_INPUT_MESSAGES == "gen_ai.input.messages"
    assert attributes.GEN_AI_TOOL_RESULT == "gen_ai.tool.result"
    assert attributes.GEN_AI_CONTEXT_TOOL_DEFINITIONS == "gen_ai.context.tool_definitions"
    assert attributes.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS == (
        "gen_ai.usage.cache_read.input_tokens"
    )
