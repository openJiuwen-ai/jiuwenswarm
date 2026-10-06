"""Offline configuration diagnostics must never disclose secret values."""

import importlib.util
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
