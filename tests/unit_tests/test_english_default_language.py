"""English defaults and explicit language choices across initialization and AgentServer."""

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common import config, utils
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter
from tests.unit_tests.server.test_gateway_adapter import _request


RESOURCES = Path(__file__).resolve().parents[2] / "jiuwenswarm" / "resources"


@pytest.mark.parametrize("name", [
    "config.yaml", "config.team.distributed.leader.yaml", "config.team.distributed.teammate.yaml",
])
def test_deployment_templates_default_to_english(name):
    raw = yaml.safe_load((RESOURCES / name).read_text(encoding="utf-8"))
    assert raw["preferred_language"] == "en"


@pytest.mark.parametrize("saved,explicit,expected", [
    (None, None, "en"), ("", None, "en"),
    ("preferred_language: zh\n", None, "zh"),
    ("preferred_language: en\n", None, "en"),
    ("preferred_language: zh\n", "en", "en"),
    ("preferred_language: en\n", "zh", "zh"),
    ("preferred_language: invalid\n", None, "en"),
])
def test_initialization_language(tmp_path, saved, explicit, expected):
    path = tmp_path / "config.yaml"
    if saved is not None:
        path.write_text(saved, encoding="utf-8")
    assert utils._resolve_preferred_language(path, explicit) == expected


def test_noninteractive_initialization_uses_english(monkeypatch):
    monkeypatch.setattr(utils, "_is_interactive", lambda: False)
    assert utils.prompt_preferred_language() == "en"


def test_fresh_workspace_persists_english_by_default(tmp_path):
    utils.prepare_workspace(overwrite=False, workspace_dir=tmp_path)
    raw = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert raw["preferred_language"] == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("saved,expected,output", [
    (None, "en", "en"), ("en", "en", "en"), ("zh", "zh", "cn"),
])
async def test_runtime_config_reaches_locale_and_agent_output(tmp_path, monkeypatch, saved, expected, output):
    raw = yaml.safe_load((RESOURCES / "config.yaml").read_text(encoding="utf-8"))
    if saved is None:
        raw.pop("preferred_language")
    else:
        raw["preferred_language"] = saved
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    resolved = config.get_config()
    assert resolved["preferred_language"] == expected

    response = await ConfigAdapter().handle(_request(ReqMethod.LOCALE_GET_CONF))
    assert response.ok is True
    assert response.payload["preferred_language"] == expected

    adapter = JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(adapter, "_active_code_config", lambda: resolved)
    assert adapter._resolve_output_language() == output
    # Reading the default must not overwrite an existing configuration file.
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == raw
