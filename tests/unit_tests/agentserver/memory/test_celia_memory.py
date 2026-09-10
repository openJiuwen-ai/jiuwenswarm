"""Tests for Celia prompt selection and memory runtime state."""

from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import read_memory_state
from jiuwenswarm.agents.harness.common.memory.external_memory_config import get_external_memory_config
from jiuwenswarm.agents.harness.common.memory.external_memory_builder import build_external_memory_rail


def test_external_builder_dispatches_celia_provider(monkeypatch):
    config = {
        "memory": {
            "engine": "external",
            "external": {"provider": "CELIA"},
        }
    }
    assert get_external_memory_config(config)["provider"] == "celia"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.external_memory_builder._build_celia_rail",
        lambda config, ext_cfg, *, session_id, request_metadata: "celia-rail",
    )
    assert build_external_memory_rail(config, session_id="conversation-a") == "celia-rail"


def test_runtime_state_is_fail_closed(tmp_path, monkeypatch):
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=1\n", encoding="utf-8")
    assert read_memory_state(str(runtime)) is True
    runtime.write_text("MEMORYSTATE=invalid\n", encoding="utf-8")
    assert read_memory_state(str(runtime)) is False
    monkeypatch.delenv("MEMORYSTATE", raising=False)
    assert read_memory_state(str(tmp_path / "missing")) is False
