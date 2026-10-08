"""Offline configuration diagnostics must never disclose secret values."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def helper():
    root = Path(__file__).resolve().parents[3]
    script = root / "jiuwenswarm/resources/agent/workspace/skills/runtime-config-check/scripts/check_env.py"
    spec = importlib.util.spec_from_file_location("runtime_config_check", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_valid_config_never_prints_values(tmp_path, helper, capsys, encoding):
    path = tmp_path / ".env"
    path.write_text("API_BASE=https://example.com/v1\nAPI_KEY=secret-marker\nMODEL_NAME=test-model\n", encoding=encoding)
    assert helper.main(["--dotenv", str(path)]) == 0
    output = capsys.readouterr().out
    assert "secret-marker" not in output
    assert "test-model" not in output
    assert helper.check(path)["utf8_bom"] == (encoding == "utf-8-sig")


@pytest.mark.parametrize("base", ["[https://example.com](https://example.com)", "https://example.com:bad", "https://secret-marker@example.com", "https://example.com/x y"])
def test_invalid_endpoint_diagnostic_is_redacted(tmp_path, helper, base):
    path = tmp_path / ".env"
    path.write_text(f'API_BASE="{base}"\nAPI_KEY=secret-marker\nMODEL_NAME=test\n', encoding="utf-8")
    result = helper.check(path)
    assert not result["valid"]
    assert "secret-marker" not in str(result)


def test_ipv6_endpoint_is_valid(tmp_path, helper):
    path = tmp_path / ".env"
    path.write_text("API_BASE=http://[::1]:8000/v1\nAPI_KEY=dummy\nMODEL_NAME=test\n", encoding="utf-8")
    assert helper.check(path)["valid"]


def test_missing_file_and_utf16_return_read_error(tmp_path, helper, capsys):
    path = tmp_path / ".env"
    assert helper.main(["--dotenv", str(path)]) == 2
    path.write_text("API_KEY=secret-marker", encoding="utf-16")
    assert helper.main(["--dotenv", str(path)]) == 2
    assert "secret-marker" not in capsys.readouterr().out


def test_missing_placeholder_and_interpolation_are_reported(tmp_path, helper):
    path = tmp_path / ".env"
    path.write_text("API_KEY=replace-locally\nMODEL_NAME=${MODEL}\n", encoding="utf-8")
    result = helper.check(path)
    assert not result["valid"]
    assert len(result["errors"]) == 3


@pytest.mark.parametrize("error", [
    OSError("secret-marker"),
    ValueError("secret-marker"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "secret-marker"),
])
def test_read_errors_emit_one_redacted_json_result(helper, monkeypatch, capsys, error):
    def fail(path):
        raise error

    monkeypatch.setattr(helper, "check", fail)
    assert helper.main(["--dotenv", "unused.env"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "valid": False, "errors": ["file cannot be read as UTF-8 dotenv"]
    }
    assert captured.out.count("\n") == 1
    assert "secret-marker" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("configured", [True, False])
def test_cli_json_protocol_and_exit_status(tmp_path, helper, capsys, configured):
    path = tmp_path / ".env"
    content = "API_BASE=https://example.com/v1\nAPI_KEY=secret-marker\nMODEL_NAME=test\n" if configured else ""
    path.write_text(content, encoding="utf-8")
    assert helper.main(["--dotenv", str(path)]) == (0 if configured else 1)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["valid"] is configured
    assert captured.out.count("\n") == 1
    assert "secret-marker" not in captured.out
    assert captured.err == ""
