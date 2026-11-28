# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the lightweight ``runtime_observability`` reader."""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.runtime_trace.config import load_runtime_trace_settings


def _write_config(
    workspace: Path,
    runtime: dict | None,
    *,
    telemetry: dict | None = None,
) -> None:
    config_dir = workspace / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if telemetry is not None:
        data["telemetry"] = telemetry
    if runtime is not None:
        data["runtime_observability"] = runtime
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8"
    )


def test_missing_config_disables_trace(tmp_path: Path) -> None:
    settings = load_runtime_trace_settings(workspace=tmp_path)
    assert settings.enabled is False
    assert settings.slow_task_ms == 1000
    assert settings.retention_days == 7
    assert settings.exporter == "none"


def test_missing_block_disables_trace(tmp_path: Path) -> None:
    _write_config(tmp_path, None)
    assert load_runtime_trace_settings(workspace=tmp_path).enabled is False


@pytest.mark.parametrize("value", [1, "true", "yes", "on", "True"])
def test_enabled_requires_yaml_true(tmp_path: Path, value: object) -> None:
    _write_config(tmp_path, {"enabled": value})
    assert load_runtime_trace_settings(workspace=tmp_path).enabled is False


def test_yaml_true_enables_trace(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True, "slow_task_ms": 25, "retention_days": 3},
    )
    settings = load_runtime_trace_settings(workspace=tmp_path)
    assert settings.enabled is True
    assert settings.slow_task_ms == 25
    assert settings.retention_days == 3


@pytest.mark.parametrize("value", [0, -5, "12", True, None])
def test_invalid_numbers_fall_back(tmp_path: Path, value: object) -> None:
    _write_config(
        tmp_path,
        {"enabled": True, "slow_task_ms": value, "retention_days": value},
    )
    settings = load_runtime_trace_settings(workspace=tmp_path)
    assert settings.slow_task_ms == 1000
    assert settings.retention_days == 7


def test_runtime_exporter_inherits_only_with_endpoint(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True, "exporter": "", "endpoint": ""},
        telemetry={
            "enabled": True,
            "exporter": "otlp",
            "endpoint": "http://collector:4317",
            "protocol": "grpc",
        },
    )
    settings = load_runtime_trace_settings(workspace=tmp_path)
    assert settings.exporter == "otlp_grpc"
    assert settings.endpoint == "http://collector:4317"


def test_inherited_otlp_http_maps_to_http_exporter(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True},
        telemetry={
            "enabled": True,
            "exporter": "otlp",
            "endpoint": "http://collector:4318/v1/traces",
            "protocol": "http",
        },
    )
    settings = load_runtime_trace_settings(workspace=tmp_path)
    assert settings.exporter == "otlp_http"
    assert settings.endpoint == "http://collector:4318/v1/traces"


def test_disabled_telemetry_does_not_inherit(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True},
        telemetry={
            "enabled": False,
            "exporter": "otlp",
            "endpoint": "http://localhost:4317",
        },
    )
    settings = load_runtime_trace_settings(workspace=tmp_path)
    assert settings.exporter == "none"
    assert settings.endpoint == ""


def test_empty_telemetry_endpoint_does_not_inherit(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True},
        telemetry={"enabled": True, "exporter": "otlp", "endpoint": ""},
    )
    settings = load_runtime_trace_settings(workspace=tmp_path)
    assert settings.exporter == "none"
    assert settings.endpoint == ""


def test_explicit_none_disables_export(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True, "exporter": "none"},
        telemetry={"enabled": True, "exporter": "otlp", "endpoint": "http://collector:4317"},
    )
    assert load_runtime_trace_settings(workspace=tmp_path).exporter == "none"


def test_unsupported_exporter_falls_back_to_none(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True, "exporter": "file"},
    )
    assert load_runtime_trace_settings(workspace=tmp_path).exporter == "none"


def test_runtime_endpoint_overrides_telemetry_endpoint(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True, "endpoint": "http://runtime:4318/v1/traces"},
        telemetry={
            "enabled": True,
            "exporter": "otlp",
            "endpoint": "http://shared:4318/v1/traces",
            "protocol": "http",
        },
    )
    settings = load_runtime_trace_settings(workspace=tmp_path)
    assert settings.exporter == "otlp_http"
    assert settings.endpoint == "http://runtime:4318/v1/traces"


def test_langfuse_keys_build_the_basic_auth_header(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "enabled": True,
            "langfuse_public_key": "pk-lf-runtime",
            "langfuse_secret_key": "sk-lf-runtime",
        },
    )
    settings = load_runtime_trace_settings(workspace=tmp_path)
    credentials = base64.b64encode(b"pk-lf-runtime:sk-lf-runtime").decode()
    # Lower case because gRPC metadata keys must be lower case.
    assert settings.headers == (("authorization", f"Basic {credentials}"),)


def test_partial_langfuse_keys_send_no_auth_header(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"enabled": True, "langfuse_public_key": "pk-lf-runtime"},
    )
    assert load_runtime_trace_settings(workspace=tmp_path).headers == ()


def test_open_ended_headers_key_is_not_read(tmp_path: Path) -> None:
    """Auth comes from the shared Langfuse keys, not from a free-form map."""
    _write_config(
        tmp_path,
        {"enabled": True, "headers": {"Authorization": "Bearer x"}},
    )
    assert load_runtime_trace_settings(workspace=tmp_path).headers == ()


def test_service_name_keeps_valid_value(tmp_path: Path) -> None:
    _write_config(tmp_path, {"enabled": True, "service_name": "my-swarm"})
    assert load_runtime_trace_settings(workspace=tmp_path).service_name == "my-swarm"


def test_service_name_falls_back_when_too_long(tmp_path: Path) -> None:
    _write_config(tmp_path, {"enabled": True, "service_name": "x" * 80})
    assert load_runtime_trace_settings(workspace=tmp_path).service_name == ""


def test_reading_config_does_not_import_core_or_otel() -> None:
    script = """
import sys
from pathlib import Path
from jiuwenswarm.runtime_trace.config import load_runtime_trace_settings
load_runtime_trace_settings(workspace=Path(sys.argv[1]))
forbidden = ("openjiuwen", "aiosqlite", "opentelemetry", "jiuwenswarm.observability")
hits = [name for name in sys.modules if name in forbidden or name.startswith(tuple(p + "." for p in forbidden))]
if hits:
    raise SystemExit("unexpected modules: " + ",".join(sorted(hits)[:10]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parent)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_reading_missing_config_creates_no_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    load_runtime_trace_settings(workspace=workspace)
    assert list(workspace.iterdir()) == []
