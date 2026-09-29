# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for external_memory_builder.

Covers the dispatch table in build_external_memory_rail() by stubbing the
agent-core provider / rail classes via sys.modules before import.
"""

import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stub agent-core modules so builder's deferred imports succeed
# ---------------------------------------------------------------------------

class _FakeRail:
    last_args = None

    def __init__(self, provider, *, user_id="__default__", scope_id="__default__",
                 session_id="__default__", thread_id=None):
        _FakeRail.last_args = {
            "provider": provider,
            "user_id": user_id,
            "scope_id": scope_id,
            "session_id": session_id,
            "thread_id": thread_id,
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


class _FakeLakeBaseProvider:
    last_init_kwargs = None
    available = True

    def __init__(self, *, api_key="", base_url="", base_id="", database_id="", timeout=60.0):
        _FakeLakeBaseProvider.last_init_kwargs = {
            "api_key": api_key, "base_url": base_url,
            "base_id": base_id, "database_id": database_id,
            "timeout": timeout,
        }
        self._api_key = api_key

    @staticmethod
    def is_available() -> bool:
        return _FakeLakeBaseProvider.available


class _FakeAgentArtsProvider:
    """Stub for OfficeAce memory providers (cloud + pc).

    The builder dispatches ``officeace_cloud`` to either
    ``OfficeAceMemoryCloudProvider`` or ``OfficeAceMemoryPcProvider`` based on
    OFFICE_ACE_DEPLOYMENT. Both are stubbed to this class so tests can assert
    construction kwargs regardless of dispatch branch.
    """
    last_init_kwargs = None
    last_class = None  # which class the builder picked: "cloud" | "pc"

    def __init__(self, **kwargs):
        _FakeAgentArtsProvider.last_init_kwargs = kwargs
        self.name = "officeace"
        self.kwargs = kwargs


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

    rails_mod = _ensure_module("openjiuwen.harness.rails")
    rails_mod.ExternalMemoryRail = _FakeRail

    rail_mod = _ensure_module("openjiuwen.harness.rails.external_memory_rail")
    rail_mod.ExternalMemoryRail = _FakeRail

    oj_mod = _ensure_module("openjiuwen.core.memory.external.openjiuwen_memory_provider")
    oj_mod.OpenJiuwenMemoryProvider = _FakeOpenjiuwenProvider

    mem0_mod = _ensure_module("openjiuwen.core.memory.external.mem0_provider")
    mem0_mod.Mem0MemoryProvider = _FakeMem0Provider

    vk_mod = _ensure_module("openjiuwen.core.memory.external.openviking_memory_provider")
    vk_mod.OpenVikingMemoryProvider = _FakeVikingProvider

    lb_mod = _ensure_module("openjiuwen.core.memory.external.lakebase_memory_provider")
    lb_mod.LakeBaseMemoryProvider = _FakeLakeBaseProvider

    aa_mod = _ensure_module("openjiuwen.core.memory.external.agentarts_memory_provider")
    aa_mod.AgentArtsMemoryProvider = _FakeAgentArtsProvider

    # OfficeAce cloud/pc providers (the two branches the officeace_cloud
    # builder dispatches to).
    oa_cloud_mod = _ensure_module(
        "openjiuwen.core.memory.external.office_ace_memory_cloud_provider"
    )
    oa_cloud_mod.OfficeAceMemoryCloudProvider = _FakeAgentArtsProvider

    oa_pc_mod = _ensure_module(
        "openjiuwen.core.memory.external.office_ace_memory_pc_provider"
    )
    oa_pc_mod.OfficeAceMemoryPcProvider = _FakeAgentArtsProvider


def _install_jiuwenswarm_stubs():
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

    # Load the real jiuwenswarm.utils (do NOT replace it in sys.modules —
    # that leaks str-returning stubs into every later test in the session).
    # Patch only the two attrs we need; the module-scoped autouse fixture
    # in this package's conftest.py restores them after this module's tests
    # finish.
    import jiuwenswarm.common.utils as utils_stub
    utils_stub.get_config_file = _get_config_file
    utils_stub.get_agent_workspace_dir = _get_agent_workspace_dir


# ---------------------------------------------------------------------------
# Save real openjiuwen modules before stubbing, restore after import so
# other test modules can still import openjiuwen subpackages.
# ---------------------------------------------------------------------------
_AGENT_CORE_STUB_MODULES = [
    "openjiuwen",
    "openjiuwen.core",
    "openjiuwen.core.memory",
    "openjiuwen.core.memory.external",
    "openjiuwen.harness",
    "openjiuwen.harness.rails",
    "openjiuwen.harness.rails.external_memory_rail",
    "openjiuwen.core.memory.external.openjiuwen_memory_provider",
    "openjiuwen.core.memory.external.mem0_provider",
    "openjiuwen.core.memory.external.openviking_memory_provider",
    "openjiuwen.core.memory.external.lakebase_memory_provider",
]

_STUB_ATTR_OVERRIDES = [
    ("openjiuwen.harness.rails", "ExternalMemoryRail"),
    ("openjiuwen.harness.rails.external_memory_rail", "ExternalMemoryRail"),
    ("openjiuwen.core.memory.external.openjiuwen_memory_provider", "OpenJiuwenMemoryProvider"),
    ("openjiuwen.core.memory.external.mem0_provider", "Mem0MemoryProvider"),
    ("openjiuwen.core.memory.external.openviking_memory_provider", "OpenVikingMemoryProvider"),
    ("openjiuwen.core.memory.external.lakebase_memory_provider", "LakeBaseMemoryProvider"),
]

_saved_sys_modules: dict = {
    name: sys.modules[name] for name in _AGENT_CORE_STUB_MODULES if name in sys.modules
}

# Save real jiuwenswarm.common.utils callables before patching
import jiuwenswarm.common.utils as _utils_mod
_saved_utils_get_config_file = _utils_mod.get_config_file
_saved_utils_get_agent_workspace_dir = _utils_mod.get_agent_workspace_dir

_install_jiuwenswarm_stubs()
_install_agent_core_stubs()

from jiuwenswarm.agents.harness.common.memory import external_memory_builder as emb  # noqa: E402
from jiuwenswarm.agents.harness.common.memory import external_memory_config as emc  # noqa: E402


# Immediately restore real modules/utils so other test modules can collect.
def _restore_agent_core_modules():
    # Restore saved originals, and remove stubs we created for modules that
    # were not in sys.modules before we started.
    for name in _AGENT_CORE_STUB_MODULES:
        if name in _saved_sys_modules:
            sys.modules[name] = _saved_sys_modules[name]
        elif name in sys.modules and not hasattr(sys.modules[name], "__path__"):
            # Only pop bare ModuleType stubs we created (no __path__ means
            # not a real package).  Leave real modules alone.
            sys.modules.pop(name, None)
    _utils_mod.get_config_file = _saved_utils_get_config_file
    _utils_mod.get_agent_workspace_dir = _saved_utils_get_agent_workspace_dir

_restore_agent_core_modules()

_SENTINEL = object()


@pytest.fixture(autouse=True)
def _isolate_agent_core_stubs():
    """Re-install stubs for this module's tests, restore after."""
    saved_attrs = {}
    for mod_name, attr_name in _STUB_ATTR_OVERRIDES:
        mod = sys.modules.get(mod_name)
        if mod is not None:
            saved_attrs[(mod_name, attr_name)] = getattr(mod, attr_name, _SENTINEL)

    saved_utils_cfg = _utils_mod.get_config_file
    saved_utils_wd = _utils_mod.get_agent_workspace_dir

    _install_agent_core_stubs()
    _install_jiuwenswarm_stubs()
    yield

    for (mod_name, attr_name), val in saved_attrs.items():
        mod = sys.modules.get(mod_name)
        if mod is not None:
            if val is _SENTINEL:
                try:
                    delattr(mod, attr_name)
                except AttributeError:
                    logger.debug("Attribute %s not found on module %s, skipping deletion", attr_name, mod_name)
            else:
                setattr(mod, attr_name, val)
    _utils_mod.get_config_file = saved_utils_cfg
    _utils_mod.get_agent_workspace_dir = saved_utils_wd
    _restore_agent_core_modules()


@pytest.fixture(autouse=True)
def reset_spy_state():
    _FakeRail.last_args = None
    _FakeOpenjiuwenProvider.last_init_kwargs = None
    _FakeMem0Provider.last_init_kwargs = None
    _FakeMem0Provider.available = True
    _FakeVikingProvider.last_init_kwargs = None
    _FakeVikingProvider.available = True
    _FakeLakeBaseProvider.last_init_kwargs = None
    _FakeLakeBaseProvider.available = True
    _FakeAgentArtsProvider.last_init_kwargs = None
    _FakeAgentArtsProvider.last_class = None
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


def test_non_agentarts_provider_ignores_session_id(monkeypatch):
    """Passing a session_id must NOT reach openjiuwen/mem0/openviking/lakebase
    rails — they keep "__default__" so their message tagging is unchanged.
    Only agentarts consumes the per-session session_id."""
    monkeypatch.setattr(
        emc, "get_embed_config",
        lambda: {"api_key": "ek", "base_url": "eb", "model": "em"},
    )
    cfg = {"memory": {"engine": "external", "external": {
        "provider": "openjiuwen",
        "openjiuwen": {"kv_type": "in_memory"},
    }}}
    assert emb.build_external_memory_rail(
        cfg, session_id="real-session-123"
    ) is not None
    # openjiuwen rail must keep "__default__", not "real-session-123"
    assert _FakeRail.last_args["session_id"] == "__default__"


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
# LakeBase branch
# ---------------------------------------------------------------------------

def test_lakebase_yaml_config(monkeypatch):
    monkeypatch.delenv("LAKEBASE_API_KEY", raising=False)
    monkeypatch.delenv("LAKEBASE_API_URL", raising=False)
    monkeypatch.delenv("LAKEBASE_MEM_BASE_ID", raising=False)
    monkeypatch.delenv("LAKEBASE_DATABASE_ID", raising=False)
    cfg = {"memory": {"external": {
        "provider": "lakebase",
        "lakebase": {
            "api_key": "lb-key",
            "base_url": "http://lakebase:9090/api/v1",
            "base_id": "mem_custom",
            "database_id": "db_test",
            "timeout": 30.0,
        },
    }}}
    assert emb.build_external_memory_rail(cfg) is not None
    assert _FakeLakeBaseProvider.last_init_kwargs == {
        "api_key": "lb-key",
        "base_url": "http://lakebase:9090/api/v1",
        "base_id": "mem_custom",
        "database_id": "db_test",
        "timeout": 30.0,
    }


def test_lakebase_env_fallback_when_yaml_empty(monkeypatch):
    monkeypatch.setenv("LAKEBASE_API_KEY", "env-lb-key")
    monkeypatch.setenv("LAKEBASE_API_URL", "http://env-lb:7070/api/v1")
    monkeypatch.setenv("LAKEBASE_MEM_BASE_ID", "env_base")
    monkeypatch.setenv("LAKEBASE_DATABASE_ID", "env_db")
    cfg = {"memory": {"external": {"provider": "lakebase", "lakebase": {}}}}
    assert emb.build_external_memory_rail(cfg) is not None
    init = _FakeLakeBaseProvider.last_init_kwargs
    assert init["api_key"] == "env-lb-key"
    assert init["base_url"] == "http://env-lb:7070/api/v1"
    assert init["base_id"] == "env_base"
    assert init["database_id"] == "env_db"


def test_lakebase_defaults_when_no_config(monkeypatch):
    monkeypatch.setenv("LAKEBASE_API_KEY", "lb-key")
    monkeypatch.delenv("LAKEBASE_API_URL", raising=False)
    monkeypatch.delenv("LAKEBASE_MEM_BASE_ID", raising=False)
    monkeypatch.delenv("LAKEBASE_DATABASE_ID", raising=False)
    cfg = {"memory": {"external": {"provider": "lakebase", "lakebase": {}}}}
    assert emb.build_external_memory_rail(cfg) is not None
    init = _FakeLakeBaseProvider.last_init_kwargs
    assert init["api_key"] == "lb-key"
    assert init["base_url"] == "http://localhost:8080/api/v1"
    assert init["base_id"] == "mem_default"
    assert init["database_id"] == "db_agent_memory"


def test_lakebase_no_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("LAKEBASE_API_KEY", raising=False)
    monkeypatch.delenv("LAKEBASE_API_URL", raising=False)
    monkeypatch.delenv("LAKEBASE_MEM_BASE_ID", raising=False)
    monkeypatch.delenv("LAKEBASE_DATABASE_ID", raising=False)
    cfg = {"memory": {"external": {"provider": "lakebase", "lakebase": {}}}}
    assert emb.build_external_memory_rail(cfg) is None


def test_lakebase_unavailable_returns_none(monkeypatch):
    _FakeLakeBaseProvider.available = False
    monkeypatch.delenv("LAKEBASE_API_KEY", raising=False)
    cfg = {"memory": {"external": {
        "provider": "lakebase",
        "lakebase": {"api_key": "lb-key"},
    }}}
    assert emb.build_external_memory_rail(cfg) is None


def test_lakebase_partial_yaml_env_blend(monkeypatch):
    """YAML values take precedence; env vars fill only the missing ones."""
    monkeypatch.setenv("LAKEBASE_API_URL", "http://env-lb:7070/api/v1")
    monkeypatch.setenv("LAKEBASE_MEM_BASE_ID", "env_base")
    cfg = {"memory": {"external": {
        "provider": "lakebase",
        "lakebase": {
            "api_key": "yaml-key",
            "base_url": "http://yaml-lb:9090/api/v1",
        },
    }}}
    assert emb.build_external_memory_rail(cfg) is not None
    init = _FakeLakeBaseProvider.last_init_kwargs
    assert init["api_key"] == "yaml-key"
    assert init["base_url"] == "http://yaml-lb:9090/api/v1"  # YAML wins
    assert init["base_id"] == "env_base"                     # env fills
    assert init["database_id"] == "db_agent_memory"                 # default fills


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
    rails_mod = sys.modules["openjiuwen.harness.rails"]
    monkeypatch.setattr(rails_mod.ExternalMemoryRail, "__init__", _boom)
    cfg = {"memory": {"external": {"provider": "mem0", "mem0": {"api_key": "k"}}}}
    assert emb.build_external_memory_rail(cfg) is None


def test_rail_import_failure_returns_none():
    orig_rails = sys.modules.get("openjiuwen.harness.rails")
    orig_rail_mod = sys.modules.get("openjiuwen.harness.rails.external_memory_rail")
    try:
        broken_rails = ModuleType("openjiuwen.harness.rails")
        sys.modules["openjiuwen.harness.rails"] = broken_rails
        broken_rail_mod = ModuleType("openjiuwen.harness.rails.external_memory_rail")
        sys.modules["openjiuwen.harness.rails.external_memory_rail"] = broken_rail_mod
        cfg = {"memory": {"external": {"provider": "mem0", "mem0": {"api_key": "k"}}}}
        assert emb.build_external_memory_rail(cfg) is None
    finally:
        if orig_rails is not None:
            sys.modules["openjiuwen.harness.rails"] = orig_rails
        else:
            sys.modules.pop("openjiuwen.harness.rails", None)
        if orig_rail_mod is not None:
            sys.modules["openjiuwen.harness.rails.external_memory_rail"] = orig_rail_mod
        else:
            sys.modules.pop("openjiuwen.harness.rails.external_memory_rail", None)


# ---------------------------------------------------------------------------
# OfficeAce memory provider — per-session credentials bound at construction.
# Builder dispatches the ``officeace_cloud`` config key to either the cloud
# or pc provider class based on OFFICE_ACE_DEPLOYMENT (both stubbed here).
# ---------------------------------------------------------------------------

def _officeace_cfg(provider_section: dict) -> dict:
    return {"memory": {"engine": "external", "external": {
        "provider": "officeace_cloud",
        "officeace_cloud": provider_section,
    }}}


def test_officeace_thread_id_threaded_into_rail(monkeypatch):
    """thread_id (业务对话 ID) is threaded from builder → rail constructor.

    PC 端 OfficeAce 记忆 sync_turn 上报 pc-threads/{thread_id}/messages 用它。
    """
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "pc")
    cfg = _officeace_cfg({"api_key": "k", "space_id": "s"})
    session_id = "relayclaw-sess-12345678"
    thread_id = "thread_abc123"
    assert emb.build_external_memory_rail(
        cfg, session_id=session_id, thread_id=thread_id
    ) is not None
    # session_id + thread_id both threaded into the rail constructor
    assert _FakeRail.last_args["session_id"] == session_id
    assert _FakeRail.last_args["thread_id"] == thread_id


def test_officeace_thread_id_none_for_non_officeace_provider(monkeypatch):
    """Non-officeace providers get thread_id=None (they don't consume it)."""
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "pc")
    cfg = {"memory": {"external": {"provider": "mem0", "mem0": {"api_key": "k"}}}}
    assert emb.build_external_memory_rail(
        cfg, session_id="sess", thread_id="thread_x"
    ) is not None
    # thread_id only flows to officeace_cloud; mem0 rail gets None
    assert _FakeRail.last_args["thread_id"] is None
    # session_id stays __default__ for non-officeace providers
    assert _FakeRail.last_args["session_id"] == "__default__"


def test_officeace_static_config_values_used(monkeypatch):
    """Static config values are read directly (no per-session runtime_config)."""
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "cloud")
    for var in ("AGENTARTS_MEMORY_BASE_URL", "AGENTARTS_MEMORY_API_KEY", "AGENTARTS_MEMORY_SPACE_ID"):
        monkeypatch.delenv(var, raising=False)
    cfg = _officeace_cfg({
        "base_url": "https://memory.example.com",
        "api_key": "static-key",
        "space_id": "static-space",
    })
    assert emb.build_external_memory_rail(cfg) is not None
    kwargs = _FakeAgentArtsProvider.last_init_kwargs
    assert kwargs["base_url"] == "https://memory.example.com"
    assert kwargs["api_key"] == "static-key"
    assert kwargs["space_id"] == "static-space"
    # No session_id -> rail falls back to "__default__"
    assert _FakeRail.last_args["session_id"] == "__default__"
    # No thread_id -> rail gets None
    assert _FakeRail.last_args["thread_id"] is None


def test_officeace_env_fallback_when_yaml_empty(monkeypatch):
    """Env vars fill in when config section is empty."""
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "cloud")
    monkeypatch.setenv("AGENTARTS_MEMORY_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AGENTARTS_MEMORY_API_KEY", "env-key")
    monkeypatch.setenv("AGENTARTS_MEMORY_SPACE_ID", "env-space")
    cfg = _officeace_cfg({})
    assert emb.build_external_memory_rail(cfg) is not None
    kwargs = _FakeAgentArtsProvider.last_init_kwargs
    assert kwargs["base_url"] == "https://env.example.com"
    assert kwargs["api_key"] == "env-key"
    assert kwargs["space_id"] == "env-space"


def test_officeace_cloud_dispatch_when_deployment_cloud(monkeypatch):
    """OFFICE_ACE_DEPLOYMENT=cloud → OfficeAceMemoryCloudProvider branch."""
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "cloud")
    cfg = _officeace_cfg({"api_key": "k", "space_id": "s"})
    assert emb.build_external_memory_rail(cfg) is not None
    # cloud provider receives space_id (pc provider does not)
    assert _FakeAgentArtsProvider.last_init_kwargs.get("space_id") == "s"


def test_officeace_pc_dispatch_when_deployment_pc(monkeypatch):
    """OFFICE_ACE_DEPLOYMENT=pc → OfficeAceMemoryPcProvider branch."""
    monkeypatch.setenv("OFFICE_ACE_DEPLOYMENT", "pc")
    cfg = _officeace_cfg({"api_key": "k", "space_id": "s"})
    assert emb.build_external_memory_rail(cfg) is not None
    # pc provider does NOT receive space_id (only base_url/api_key/actor_id)
    assert "space_id" not in _FakeAgentArtsProvider.last_init_kwargs


def test_officeace_pc_dispatch_when_deployment_unset(monkeypatch):
    """Unset OFFICE_ACE_DEPLOYMENT defaults to pc branch."""
    monkeypatch.delenv("OFFICE_ACE_DEPLOYMENT", raising=False)
    cfg = _officeace_cfg({"api_key": "k"})
    assert emb.build_external_memory_rail(cfg) is not None
    assert "space_id" not in _FakeAgentArtsProvider.last_init_kwargs
