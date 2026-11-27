# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for external_memory_builder.

Covers the dispatch table in build_external_memory_rail() by stubbing the
agent-core provider / rail classes via sys.modules before import.
"""

import sys
from pathlib import Path
from types import ModuleType

import pytest


# ---------------------------------------------------------------------------
# Stub agent-core modules so builder's deferred imports succeed
# ---------------------------------------------------------------------------

class _FakeRail:
    last_args = None

    def __init__(self, provider, *, user_id="__default__", scope_id="__default__",
                 session_id="__default__"):
        _FakeRail.last_args = {
            "provider": provider,
            "user_id": user_id,
            "scope_id": scope_id,
        }
        self.provider = provider


class _FakeOpenjiuwenProvider:
    last_init_kwargs = None

    def __init__(self, config=None, **kwargs):
        _FakeOpenjiuwenProvider.last_init_kwargs = {"config": config, **kwargs}
        self._config = config

    @staticmethod
    def is_available() -> bool:
        return True


class _FakeMem0Provider:
    last_init_kwargs = None
    available = True

    def __init__(self, *, api_key="", user_id="", agent_id="", rerank=False):
        _FakeMem0Provider.last_init_kwargs = {
            "api_key": api_key, "user_id": user_id,
            "agent_id": agent_id, "rerank": rerank,
        }
        self._api_key = api_key

    @staticmethod
    def is_available() -> bool:
        return _FakeMem0Provider.available


class _FakeVikingProvider:
    last_init_kwargs = None
    available = True

    def __init__(self, *, endpoint="", api_key="", account="", user=""):
        _FakeVikingProvider.last_init_kwargs = {
            "endpoint": endpoint, "api_key": api_key,
            "account": account, "user": user,
        }
        self._endpoint = endpoint

    @staticmethod
    def is_available() -> bool:
        return _FakeVikingProvider.available


def _ensure_module(name: str) -> ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = ModuleType(name)
        sys.modules[name] = mod
    return mod


def _install_agent_core_stubs():
    for pkg in [
        "openjiuwen",
        "openjiuwen.core",
        "openjiuwen.core.memory",
        "openjiuwen.core.memory.external",
        "openjiuwen.harness",
        "openjiuwen.harness.rails",
    ]:
        _ensure_module(pkg)

    rail_mod = _ensure_module("openjiuwen.harness.rails.external_memory_rail")
    rail_mod.ExternalMemoryRail = _FakeRail

    oj_mod = _ensure_module("openjiuwen.core.memory.external.openjiuwen_memory_provider")
    oj_mod.OpenJiuwenMemoryProvider = _FakeOpenjiuwenProvider

    mem0_mod = _ensure_module("openjiuwen.core.memory.external.mem0_provider")
    mem0_mod.Mem0MemoryProvider = _FakeMem0Provider

    vk_mod = _ensure_module("openjiuwen.core.memory.external.openviking_memory_provider")
    vk_mod.OpenVikingMemoryProvider = _FakeVikingProvider


def _install_jiuwenclaw_stubs():
    ruamel = _ensure_module("ruamel")
    ruamel_yaml = _ensure_module("ruamel.yaml")
    ruamel.yaml = ruamel_yaml
    if not hasattr(ruamel_yaml, "YAML"):
        class _YAML:
            def __init__(self, *a, **k):
                pass

            @staticmethod
            def load(*a, **k):
                return {}

            @staticmethod
            def dump(*a, **k):
                pass
        ruamel_yaml.YAML = _YAML

    def _get_config_file():
        return Path("/tmp/test_config.yaml")

    def _get_agent_workspace_dir():
        return Path("/tmp/test_workspace")

    # Load the real jiuwenclaw.utils (do NOT replace it in sys.modules —
    # that leaks str-returning stubs into every later test in the session).
    # Patch only the two attrs we need; the module-scoped autouse fixture
    # in this package's conftest.py restores them after this module's tests
    # finish.
    import jiuwenclaw.utils as utils_stub
    utils_stub.get_config_file = _get_config_file
    utils_stub.get_agent_workspace_dir = _get_agent_workspace_dir


_install_jiuwenclaw_stubs()
_install_agent_core_stubs()

from jiuwenclaw.agentserver.memory import external_memory_builder as emb  # noqa: E402
from jiuwenclaw.agentserver.memory import external_memory_config as emc  # noqa: E402


@pytest.fixture(autouse=True)
def reset_spy_state():
    _FakeRail.last_args = None
    _FakeOpenjiuwenProvider.last_init_kwargs = None
    _FakeMem0Provider.last_init_kwargs = None
    _FakeMem0Provider.available = True
    _FakeVikingProvider.last_init_kwargs = None
    _FakeVikingProvider.available = True
    yield


# ---------------------------------------------------------------------------
# Disabled / empty cases
# ---------------------------------------------------------------------------

def test_empty_provider_returns_none():
    cfg = {"memory": {"engine": "external", "external": {"provider": ""}}}
    assert emb.build_external_memory_rail(cfg) is None


def test_engine_builtin_still_builds_caller_must_gate():
    # Builder looks only at provider name; engine gating is the caller's
    # responsibility via is_external_memory_enabled(). Confirm no short-circuit.
    cfg = {"memory": {"engine": "builtin",
                      "external": {"provider": "mem0", "mem0": {"api_key": "k"}}}}
    assert emb.build_external_memory_rail(cfg) is not None


# ---------------------------------------------------------------------------
# OpenJiuwen branch
# ---------------------------------------------------------------------------

def test_openjiuwen_happy_path(monkeypatch):
    monkeypatch.setattr(
        emc, "get_embed_config",
        lambda: {"api_key": "ek", "base_url": "eb", "model": "em"},
    )
    cfg = {"memory": {"engine": "external", "external": {
        "provider": "openjiuwen",
        "user_id": "alice",
        "scope_id": "proj-a",
        "openjiuwen": {"kv_type": "in_memory"},
    }}}
    assert emb.build_external_memory_rail(cfg) is not None

    init = _FakeOpenjiuwenProvider.last_init_kwargs
    assert isinstance(init["config"], dict)
    assert init["config"]["kv"]["backend"] == "in_memory"
    assert init["config"]["embedding"]["model_name"] == "em"

    rail_args = _FakeRail.last_args
    assert rail_args["user_id"] == "alice"
    assert rail_args["scope_id"] == "proj-a"


# ---------------------------------------------------------------------------
# Mem0 branch
# ---------------------------------------------------------------------------

def test_mem0_yaml_takes_precedence(monkeypatch):
    for var in ("MEM0_API_KEY", "MEM0_USER_ID", "MEM0_AGENT_ID"):
        monkeypatch.delenv(var, raising=False)
    cfg = {"memory": {"engine": "external", "external": {
        "provider": "mem0",
        "mem0": {
            "api_key": "yaml-key",
            "user_id": "yaml-user",
            "agent_id": "yaml-agent",
            "rerank": False,
        },
    }}}
    assert emb.build_external_memory_rail(cfg) is not None
    assert _FakeMem0Provider.last_init_kwargs == {
        "api_key": "yaml-key",
        "user_id": "yaml-user",
        "agent_id": "yaml-agent",
        "rerank": False,
    }


def test_mem0_env_fallback_when_yaml_empty(monkeypatch):
    monkeypatch.setenv("MEM0_API_KEY", "env-key")
    monkeypatch.setenv("MEM0_USER_ID", "env-user")
    monkeypatch.setenv("MEM0_AGENT_ID", "env-agent")
    cfg = {"memory": {"external": {"provider": "mem0", "mem0": {}}}}
    assert emb.build_external_memory_rail(cfg) is not None
    init = _FakeMem0Provider.last_init_kwargs
    assert init["api_key"] == "env-key"
    assert init["user_id"] == "env-user"
    assert init["agent_id"] == "env-agent"


def test_mem0_unavailable_returns_none(monkeypatch):
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    _FakeMem0Provider.available = False
    cfg = {"memory": {"external": {"provider": "mem0", "mem0": {"api_key": "k"}}}}
    assert emb.build_external_memory_rail(cfg) is None


# ---------------------------------------------------------------------------
# OpenViking branch
# ---------------------------------------------------------------------------

def test_openviking_yaml_config(monkeypatch):
    monkeypatch.delenv("OPENVIKING_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENVIKING_API_KEY", raising=False)
    cfg = {"memory": {"external": {
        "provider": "openviking",
        "openviking": {
            "endpoint": "http://viking:1933",
            "api_key": "vk-key",
            "account": "acct",
            "user": "u1",
        },
    }}}
    assert emb.build_external_memory_rail(cfg) is not None
    assert _FakeVikingProvider.last_init_kwargs == {
        "endpoint": "http://viking:1933",
        "api_key": "vk-key",
        "account": "acct",
        "user": "u1",
    }


def test_openviking_env_fallback(monkeypatch):
    monkeypatch.setenv("OPENVIKING_ENDPOINT", "http://env:9000")
    cfg = {"memory": {"external": {"provider": "openviking", "openviking": {}}}}
    assert emb.build_external_memory_rail(cfg) is not None
    assert _FakeVikingProvider.last_init_kwargs["endpoint"] == "http://env:9000"


def test_openviking_unavailable_returns_none(monkeypatch):
    _FakeVikingProvider.available = False
    monkeypatch.delenv("OPENVIKING_ENDPOINT", raising=False)
    cfg = {"memory": {"external": {"provider": "openviking", "openviking": {}}}}
    assert emb.build_external_memory_rail(cfg) is None


# ---------------------------------------------------------------------------
# Plugin branch (discovery not yet implemented)
# ---------------------------------------------------------------------------

def test_plugin_unavailable_returns_none():
    cfg = {"memory": {"external": {"provider": "honcho"}}}
    assert emb.build_external_memory_rail(cfg) is None


# ---------------------------------------------------------------------------
# Failure handling — graceful degradation
# ---------------------------------------------------------------------------

def test_provider_raises_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(_FakeMem0Provider, "__init__", _boom)
    cfg = {"memory": {"external": {"provider": "mem0", "mem0": {"api_key": "k"}}}}
    assert emb.build_external_memory_rail(cfg) is None


def test_rail_construction_failure_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("rail boom")
    monkeypatch.setattr(_FakeRail, "__init__", _boom)
    cfg = {"memory": {"external": {"provider": "mem0", "mem0": {"api_key": "k"}}}}
    assert emb.build_external_memory_rail(cfg) is None


def test_rail_import_failure_returns_none():
    orig = sys.modules.pop("openjiuwen.harness.rails.external_memory_rail")
    try:
        broken = ModuleType("openjiuwen.harness.rails.external_memory_rail")
        sys.modules["openjiuwen.harness.rails.external_memory_rail"] = broken
        cfg = {"memory": {"external": {"provider": "mem0", "mem0": {"api_key": "k"}}}}
        assert emb.build_external_memory_rail(cfg) is None
    finally:
        sys.modules["openjiuwen.harness.rails.external_memory_rail"] = orig
