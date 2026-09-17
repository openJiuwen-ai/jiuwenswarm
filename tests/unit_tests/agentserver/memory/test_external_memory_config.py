# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for external_memory_config.

Covers engine policy gates, external config reader, and OpenJiuwen provider
config mapping. Uses sys.modules stubs to avoid heavy deps at import time.
"""

import sys
from pathlib import Path
from types import ModuleType

import pytest


# ---------------------------------------------------------------------------
# Stub heavy deps before importing the module under test
# ---------------------------------------------------------------------------

def _ensure_module(name: str) -> ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = ModuleType(name)
        sys.modules[name] = mod
    return mod


def _install_stubs():
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


_install_stubs()


def _get_config_file():
    return Path("/tmp/test_config.yaml")


def _get_agent_workspace_dir():
    return Path("/tmp/test_workspace")


# Load the real jiuwenswarm.utils (do NOT replace it in sys.modules — that
# leaks str-returning stubs into every later test in the session). Patch
# only the two attrs we need; restore immediately after importing the
# module under test so other test modules can collect.
import jiuwenswarm.common.utils as utils_stub  # noqa: E402

_saved_get_config_file = utils_stub.get_config_file
_saved_get_agent_workspace_dir = utils_stub.get_agent_workspace_dir

utils_stub.get_config_file = _get_config_file
utils_stub.get_agent_workspace_dir = _get_agent_workspace_dir

from jiuwenswarm.agents.harness.common.memory import external_memory_config as emc  # noqa: E402

utils_stub.get_config_file = _saved_get_config_file
utils_stub.get_agent_workspace_dir = _saved_get_agent_workspace_dir


@pytest.fixture(autouse=True)
def _isolate_utils_stubs():
    """Re-apply utils stubs for this module's tests, restore after."""
    utils_stub.get_config_file = _get_config_file
    utils_stub.get_agent_workspace_dir = _get_agent_workspace_dir
    yield
    utils_stub.get_config_file = _saved_get_config_file
    utils_stub.get_agent_workspace_dir = _saved_get_agent_workspace_dir


# ---------------------------------------------------------------------------
# get_memory_engine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("builtin", "builtin"),
    ("external", "external"),
    ("both", "both"),
    ("none", "none"),
    ("BUILTIN", "builtin"),
    ("  both ", "both"),
])
def test_get_memory_engine_valid_values(value, expected):
    assert emc.get_memory_engine({"memory": {"engine": value}}) == expected


@pytest.mark.parametrize("cfg", [
    {"memory": {"engine": "weird"}},
    {"memory": {"engine": None}},
    {"memory": {}},
    {},
    None,
])
def test_get_memory_engine_falls_back_to_builtin(cfg):
    assert emc.get_memory_engine(cfg) == "builtin"


# ---------------------------------------------------------------------------
# Engine gates truth table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine, builtin_ok, external_ok", [
    ("builtin", True, False),
    ("external", False, True),
    ("both", True, True),
    ("none", False, False),
])
def test_engine_gates_truth_table(engine, builtin_ok, external_ok):
    cfg = {"memory": {"engine": engine}}
    assert emc.is_builtin_memory_allowed(cfg) is builtin_ok
    assert emc.is_external_memory_allowed(cfg) is external_ok


# ---------------------------------------------------------------------------
# get_external_memory_config
# ---------------------------------------------------------------------------

def test_external_config_defaults_when_missing():
    out = emc.get_external_memory_config({"memory": {}})
    assert out["provider"] == ""
    assert out["user_id"] == "__default__"
    assert out["scope_id"] == "__default__"
    assert out["allowed_plugins"] == []
    assert out["openjiuwen"] == {}
    assert out["mem0"] == {}
    assert out["openviking"] == {}


def test_external_config_values_passthrough():
    cfg = {"memory": {"external": {
        "provider": "mem0",
        "user_id": "alice",
        "scope_id": "proj-a",
        "mem0": {"api_key": "sk-xxx"},
    }}}
    out = emc.get_external_memory_config(cfg)
    assert out["provider"] == "mem0"
    assert out["user_id"] == "alice"
    assert out["scope_id"] == "proj-a"
    assert out["mem0"] == {"api_key": "sk-xxx"}


def test_external_config_provider_whitespace_stripped():
    cfg = {"memory": {"external": {"provider": "  openjiuwen  "}}}
    assert emc.get_external_memory_config(cfg)["provider"] == "openjiuwen"


def test_external_config_malformed_section_tolerated():
    cfg = {"memory": {"external": "not-a-dict"}}
    assert emc.get_external_memory_config(cfg)["provider"] == ""


# ---------------------------------------------------------------------------
# is_external_memory_enabled  (engine gate AND provider non-empty)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg, expected", [
    # engine gate allows, provider present → enabled
    ({"memory": {"engine": "external", "external": {"provider": "mem0"}}}, True),
    ({"memory": {"engine": "both", "external": {"provider": "openjiuwen"}}}, True),
    # engine gate blocks, regardless of provider
    ({"memory": {"engine": "builtin", "external": {"provider": "mem0"}}}, False),
    ({"memory": {"engine": "none", "external": {"provider": "mem0"}}}, False),
    # engine allows but provider empty / missing
    ({"memory": {"engine": "external", "external": {"provider": ""}}}, False),
    ({"memory": {"engine": "external"}}, False),
])
def test_is_external_memory_enabled(cfg, expected):
    assert emc.is_external_memory_enabled(cfg) is expected


# ---------------------------------------------------------------------------
# build_openjiuwen_provider_config  (mapping to provider-expected shape)
# ---------------------------------------------------------------------------

def _patch_embed(monkeypatch, api_key="k", base_url="u", model="m"):
    monkeypatch.setattr(
        emc, "get_embed_config",
        lambda: {"api_key": api_key, "base_url": base_url, "model": model},
    )


def test_openjiuwen_provider_config_defaults(monkeypatch):
    _patch_embed(monkeypatch)
    out, _ = emc.build_openjiuwen_provider_config({"openjiuwen": {}})
    assert out["kv"]["backend"] == "shelve"
    assert out["vector"]["backend"] == "chroma"
    assert out["db"]["backend"] == "sqlite"
    assert "kv" in out["kv"]["path"] or out["kv"]["path"].endswith("kv")
    assert out["embedding"]["model_name"] == "m"
    assert out["embedding"]["api_key"] == "k"
    # scope_config depends on the resolved config.yaml models section and is
    # exercised separately; this test only asserts provider_config shape.


def test_openjiuwen_provider_config_overrides_applied(monkeypatch):
    _patch_embed(monkeypatch)
    ext_cfg = {"openjiuwen": {
        "kv_type": "in_memory",
        "kv_path": "/custom/kv",
        "vector_type": "chroma",
        "vector_persist_dir": "/custom/vec",
        "db_type": "sqlite",
        "db_path": "/custom/db.sqlite",
    }}
    out, _ = emc.build_openjiuwen_provider_config(ext_cfg)
    assert out["kv"] == {"backend": "in_memory", "path": "/custom/kv"}
    assert out["vector"]["persist_directory"] == "/custom/vec"
    assert out["db"]["path"] == "/custom/db.sqlite"


def test_openjiuwen_provider_config_missing_embedding_does_not_raise(monkeypatch):
    # When embed is absent the function must NOT raise; just warn.
    monkeypatch.setattr(
        emc, "get_embed_config",
        lambda: {"api_key": "", "base_url": "", "model": ""},
    )
    for var in ("EMBED_API_KEY", "EMBED_BASE_URL", "EMBED_MODEL"):
        monkeypatch.delenv(var, raising=False)
    out, _ = emc.build_openjiuwen_provider_config({"openjiuwen": {}})
    assert out["embedding"]["model_name"] == ""


def test_openjiuwen_provider_config_missing_subsection_uses_defaults(monkeypatch):
    _patch_embed(monkeypatch)
    out, _ = emc.build_openjiuwen_provider_config({})
    assert out["kv"]["backend"] == "shelve"
    assert out["vector"]["backend"] == "chroma"
    assert out["db"]["backend"] == "sqlite"
