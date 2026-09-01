from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.message import ReqMethod


def test_personal_context_api_inventory_and_legacy_removal() -> None:
    methods = {
        item.value for item in ReqMethod if item.value.startswith("personal_context.")
    }
    legacy_prefix = "p" + "cs."
    removed_methods = {
        "personal_context.runtime." + "start",
        "personal_context.runtime." + "stop",
        "personal_context.fetch.start_" + "scheduler",
        "personal_context.fetch.stop_" + "scheduler",
    }

    assert len(methods) == 25
    assert methods.isdisjoint(removed_methods)
    assert not any(item.value.startswith(legacy_prefix) for item in ReqMethod)


def test_personal_context_host_contract() -> None:
    from jiuwenswarm.server.personal_context import PersonalContextHostAPI

    legacy_module = ".".join(("jiuwenswarm", "server", "proactive" + "_" + "context"))

    assert PersonalContextHostAPI.__name__ == "PersonalContextHostAPI"
    assert importlib.util.find_spec(legacy_module) is None


@pytest.mark.asyncio
async def test_legacy_config_file_is_not_read(tmp_path: Path) -> None:
    from jiuwenswarm.server.personal_context import PersonalContextHostAPI

    home = tmp_path / "home"
    home.mkdir()
    legacy_config = home / ("p" + "cs.yaml")
    legacy_config.write_text("enabled: false\n", encoding="utf-8")
    host = PersonalContextHostAPI(home=home)

    await host.start()
    status = await host.get_status()

    # Legacy pcs.yaml is ignored: a fresh default config is bootstrapped
    # (collection_enabled=True) instead of inheriting legacy "enabled: false".
    assert status.configured is True
    assert status.collection_enabled is True
    assert host._config_path == home / "personal_context.yaml"
    assert host._config_path.is_file()
    assert legacy_config.is_file()
