# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for the ``jiuwenswarm-doctor`` CLI (``jiuwenswarm.doctor_cli``)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm import doctor_cli
from jiuwenswarm.doctor_cli import (
    DoctorContext,
    _check_config_yaml,
    _check_core,
    _check_env_file,
    _check_external_tools,
    _check_mcp,
    _check_model_apis,
    _check_services,
    _check_python_version,
    main,
    render_json,
    render_text,
    run_checks,
)

pytestmark = pytest.mark.unit


def _ctx(**kwargs) -> DoctorContext:
    defaults = dict(
        lang="en", config=None, config_error=None, native_result=None, native_error=None
    )
    defaults.update(kwargs)
    return DoctorContext(**defaults)


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #


def test_python_version_fails_below_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor_cli.sys, "version_info", SimpleNamespace(major=3, minor=10)
    )
    result = _check_python_version(_ctx())[0]
    assert result.status == doctor_cli.STATUS_FAIL


def test_python_version_warns_above_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor_cli.sys, "version_info", SimpleNamespace(major=3, minor=14)
    )
    result = _check_python_version(_ctx())[0]
    assert result.status == doctor_cli.STATUS_WARN


def test_python_version_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor_cli.sys, "version_info", SimpleNamespace(major=3, minor=11)
    )
    result = _check_python_version(_ctx())[0]
    assert result.status == doctor_cli.STATUS_OK


def test_install_mode_source_when_distribution_missing(monkeypatch) -> None:
    import importlib.metadata

    def _raise(*args, **kwargs):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(doctor_cli.importlib.metadata, "distribution", _raise)
    result = doctor_cli._check_install_mode(_ctx())[0]
    assert result.status == doctor_cli.STATUS_OK
    assert "source" in result.message


def test_core_maps_native_results_by_kind() -> None:
    ctx = _ctx(
        native_result={
            "checks": [
                {
                    "name": "data_directory",
                    "display_name": "数据目录",
                    "kind": "filesystem",
                    "status": "ok",
                    "message": "/tmp",
                },
                {
                    "name": "grpc._cython.cygrpc",
                    "display_name": "cygrpc",
                    "kind": "native_import",
                    "status": "failed",
                    "message": "DLL load failed",
                },
                {
                    "name": "numpy",
                    "display_name": "NumPy",
                    "kind": "native_import",
                    "status": "ok",
                    "message": "",
                },
            ]
        }
    )
    results = _check_core(ctx)
    by_name = {r.id: r for r in results}
    assert by_name["data_directory"].category == "environment"
    assert by_name["data_directory"].status == doctor_cli.STATUS_OK
    assert by_name["grpc._cython.cygrpc"].category == "native"
    assert by_name["grpc._cython.cygrpc"].status == doctor_cli.STATUS_FAIL
    assert by_name["grpc._cython.cygrpc"].hint


def test_core_reports_when_native_unavailable() -> None:
    ctx = _ctx(native_result=None, native_error="boom")
    results = _check_core(ctx)
    assert results[0].status == doctor_cli.STATUS_FAIL
    assert "boom" in results[0].message


def test_config_yaml_ok(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor_cli, "_data_dir", lambda: tmp_path)
    ctx = _ctx(config={})
    result = _check_config_yaml(ctx)[0]
    assert result.status == doctor_cli.STATUS_OK


def test_config_yaml_parse_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor_cli, "_data_dir", lambda: tmp_path)
    ctx = _ctx(config=None, config_error="failed to parse")
    result = _check_config_yaml(ctx)[0]
    assert result.status == doctor_cli.STATUS_FAIL
    assert result.hint


def test_config_yaml_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor_cli, "_data_dir", lambda: tmp_path)
    ctx = _ctx(config=None, config_error=None)
    result = _check_config_yaml(ctx)[0]
    assert result.status == doctor_cli.STATUS_FAIL


def test_env_file_missing_warns(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor_cli, "_data_dir", lambda: tmp_path)
    result = _check_env_file(_ctx())[0]
    assert result.status == doctor_cli.STATUS_WARN


def test_env_file_present(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor_cli, "_data_dir", lambda: tmp_path)
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / ".env").write_text("X=1", encoding="utf-8")
    result = _check_env_file(_ctx())[0]
    assert result.status == doctor_cli.STATUS_OK


def test_external_tools(monkeypatch) -> None:
    def _which(cmd: str):
        return f"/usr/bin/{cmd}" if cmd == "git" else None

    monkeypatch.setattr(doctor_cli.shutil, "which", _which)
    results = _check_external_tools(_ctx())
    by_id = {r.id: r for r in results}
    assert by_id["tool:git"].status == doctor_cli.STATUS_OK
    assert by_id["tool:node"].status == doctor_cli.STATUS_WARN
    assert by_id["tool:ffmpeg"].status == doctor_cli.STATUS_WARN


def test_model_apis_placeholder_warns() -> None:
    ctx = _ctx(
        config={
            "models": {
                "defaults": [
                    {
                        "model_client_config": {
                            "api_base": "https://example.com/v1",
                            "model_name": "your-model-name",
                            "api_key": "sk-xxxxxxxxx",
                        }
                    }
                ]
            }
        }
    )
    result = _check_model_apis(ctx)[0]
    assert result.status == doctor_cli.STATUS_WARN


def test_model_apis_reachable(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_probe_http",
        lambda url, api_key, timeout: ("ok", "reachable (HTTP 200, 12 ms)"),
    )
    ctx = _ctx(
        config={
            "models": {
                "defaults": [
                    {
                        "model_client_config": {
                            "api_base": "https://api.openai.com/v1",
                            "model_name": "gpt",
                            "api_key": "sk-real",
                        }
                    }
                ]
            }
        }
    )
    result = _check_model_apis(ctx)[0]
    assert result.status == doctor_cli.STATUS_OK
    assert "key set" in result.message


def test_model_apis_unreachable_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor_cli,
        "_probe_http",
        lambda url, api_key, timeout: ("fail", "URLError: timeout"),
    )
    ctx = _ctx(
        config={
            "models": {
                "defaults": [
                    {
                        "model_client_config": {
                            "api_base": "https://api.openai.com/v1",
                            "model_name": "gpt",
                            "api_key": "sk-real",
                        }
                    }
                ]
            }
        }
    )
    result = _check_model_apis(ctx)[0]
    assert result.status == doctor_cli.STATUS_FAIL
    assert result.hint


def test_model_apis_deep_passes_key(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _fake_probe(url, api_key, timeout):
        seen["api_key"] = api_key
        return ("ok", "reachable (HTTP 200)")

    monkeypatch.setattr(doctor_cli, "_probe_http", _fake_probe)
    ctx = _ctx(
        deep=True,
        config={
            "models": {
                "defaults": [
                    {
                        "model_client_config": {
                            "api_base": "https://api.openai.com/v1",
                            "model_name": "gpt",
                            "api_key": "sk-real",
                        }
                    }
                ]
            }
        },
    )
    _check_model_apis(ctx)
    assert seen["api_key"] == "sk-real"


def test_model_apis_no_key_not_passed_to_deep(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _fake_probe(url, api_key, timeout):
        seen["api_key"] = api_key
        return ("ok", "reachable")

    monkeypatch.setattr(doctor_cli, "_probe_http", _fake_probe)
    ctx = _ctx(
        deep=True,
        config={
            "models": {
                "defaults": [
                    {
                        "model_client_config": {
                            "api_base": "https://api.openai.com/v1",
                            "model_name": "gpt",
                            "api_key": "",
                        }
                    }
                ]
            }
        },
    )
    _check_model_apis(ctx)
    assert seen["api_key"] is None


def test_services(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor_cli, "_port_open", lambda port, timeout=0.3: port == 19001
    )
    results = _check_services(_ctx())
    by_id = {r.id: r for r in results}
    assert "listening" in by_id["service:gateway"].message
    assert "not running" in by_id["service:web"].message


def test_mcp_none(monkeypatch) -> None:
    fake = types.ModuleType("jiuwenswarm.common.mcp_config")
    fake.extract_enabled_mcp_server_entries = lambda: []
    monkeypatch.setitem(sys.modules, "jiuwenswarm.common.mcp_config", fake)
    result = _check_mcp(_ctx())[0]
    assert result.status == doctor_cli.STATUS_OK


def test_mcp_stdio_command(monkeypatch) -> None:
    fake = types.ModuleType("jiuwenswarm.common.mcp_config")
    fake.extract_enabled_mcp_server_entries = lambda: [
        {"name": "ssh", "transport": "stdio", "command": "ssh-mcp-server"}
    ]
    monkeypatch.setitem(sys.modules, "jiuwenswarm.common.mcp_config", fake)
    monkeypatch.setattr(doctor_cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    result = _check_mcp(_ctx())[0]
    assert result.status == doctor_cli.STATUS_OK


def test_mcp_http_probe(monkeypatch) -> None:
    fake = types.ModuleType("jiuwenswarm.common.mcp_config")
    fake.extract_enabled_mcp_server_entries = lambda: [
        {
            "name": "gildata",
            "transport": "http",
            "url": "https://mcp.example.com/sse?token=SECRET",
        }
    ]
    monkeypatch.setitem(sys.modules, "jiuwenswarm.common.mcp_config", fake)
    monkeypatch.setattr(
        doctor_cli,
        "_probe_http",
        lambda url, api_key, timeout: ("ok", "reachable (HTTP 200)"),
    )
    result = _check_mcp(_ctx())[0]
    assert result.status == doctor_cli.STATUS_OK
    assert "token" not in result.message


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _ok() -> list:
    return [
        doctor_cli._result("a", "environment", "Python version", "ok", "3.11.9"),
        doctor_cli._result(
            "b", "environment", ".env", "warn", "not found", "create it"
        ),
        doctor_cli._result("c", "models", "gpt", "fail", "timeout", "check proxy"),
    ]


def test_render_text_tty() -> None:
    out = render_text(_ok(), "en", use_color=False, unicode_symbols=True, verbose=False)
    assert "✓" in out
    assert "Environment" in out
    assert "1 issue(s), 1 warning(s)" in out


def test_render_text_ascii() -> None:
    out = render_text(
        _ok(), "en", use_color=False, unicode_symbols=False, verbose=False
    )
    assert "[ OK ]" in out
    assert "[FAIL]" in out


def test_render_text_chinese() -> None:
    out = render_text(
        _ok(), "zh", use_color=False, unicode_symbols=False, verbose=False
    )
    assert "环境" in out


def test_render_json_schema() -> None:
    payload = render_json(_ok())
    assert payload["type"] == "doctor_cli_result"
    assert payload["status"] == "error"
    assert payload["summary"] == {"ok": 1, "warn": 1, "fail": 1}
    assert payload["checks"][0]["id"] == "a"


# --------------------------------------------------------------------------- #
# main() / exit codes
# --------------------------------------------------------------------------- #


def _patch_main(monkeypatch, results) -> None:
    monkeypatch.setattr(doctor_cli, "_run_native", lambda: ({}, None))
    monkeypatch.setattr(doctor_cli, "_read_config_yaml", lambda: (None, None))
    monkeypatch.setattr(doctor_cli, "run_checks", lambda ctx: results)


def test_main_ok_exit_zero(monkeypatch, capsys) -> None:
    _patch_main(monkeypatch, [doctor_cli._result("a", "environment", "Python", "ok")])
    assert main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "doctor_cli_result"


def test_main_fail_exit_ten(monkeypatch, capsys) -> None:
    _patch_main(monkeypatch, [doctor_cli._result("a", "models", "gpt", "fail")])
    assert main([]) == 10


def test_main_warn_not_strict_exit_zero(monkeypatch, capsys) -> None:
    _patch_main(monkeypatch, [doctor_cli._result("a", "environment", ".env", "warn")])
    assert main([]) == 0


def test_main_strict_warn_exit_ten(monkeypatch, capsys) -> None:
    _patch_main(monkeypatch, [doctor_cli._result("a", "environment", ".env", "warn")])
    assert main(["--strict"]) == 10


def test_main_internal_error_exit_eleven(monkeypatch, capsys) -> None:
    def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor_cli, "_run_native", _boom)
    assert main([]) == 11
    assert "kaboom" in capsys.readouterr().out


def test_main_output_writes_json(monkeypatch, tmp_path: Path) -> None:
    _patch_main(monkeypatch, [doctor_cli._result("a", "environment", "Python", "ok")])
    out = tmp_path / "doctor.json"
    assert main(["--json", "--output", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["type"] == "doctor_cli_result"


def test_run_checks_guards_factory_exception(monkeypatch) -> None:
    def _boom(ctx):
        raise ValueError("bad")

    monkeypatch.setattr(doctor_cli, "_CHECK_FACTORIES", [_boom])
    results = run_checks(_ctx())
    assert results[0].status == doctor_cli.STATUS_FAIL
    assert results[0].category == "internal"
