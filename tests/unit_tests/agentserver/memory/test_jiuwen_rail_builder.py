# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwen_rail_builder (builtin jiuwen memory variant).

Covers both mount modes (http / sdk) by stubbing the site-packages rail /
provider classes at call time. The adapter is imported ONCE at module level
(skip the whole module when absent) and only its class attribute is swapped
per test — the builder's lazy dotted from-import is flaky when the module
isn't already cached in sys.modules (intermittent ModuleNotFoundError in the
pytest process after openjiuwen imports), so the tests pre-cache the real
module and never exercise the finder during tests.

sdk 模式的内核配置优先级（2026-10-17 起 builder 不再自行缝合）：
``memory.builtin.jiuwen.sdk`` 内联的两层命名空间 dict > provider 内置 agent 场景
默认装配（裸构造）；``sdk.config_path`` 为历史字段，已不再读取。
"""

import pytest

# Import the adapter EARLY (clean import state, before jiuwenswarm/openjiuwen
# module-graph side effects) and skip this module when it is not installed.
adapter_mod = pytest.importorskip(
    "jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider"
)

from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as jrb  # noqa: E402


class _FakeJiuwenRail:
    last_args = None

    def __init__(
        self,
        provider,
        *,
        user_id="__default__",
        scope_id="__default__",
        agent_id="jiuwenswarm",
        session_id="",
        session_end_extract=False,
    ):
        _FakeJiuwenRail.last_args = {
            "provider": provider,
            "user_id": user_id,
            "scope_id": scope_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "session_end_extract": session_end_extract,
        }
        self.provider = provider


class _FakeAgentMemoryProvider:
    last_init_kwargs = None
    available = True

    def __init__(
        self,
        *,
        base_url=None,
        config_path=None,
        config_dict=None,
        security_provider=None,
        api_key="",
        user_id="",
        agent_id="",
        space="",
        identity_org="local",
        identity_user="developer",
        **_kwargs,
    ):
        _FakeAgentMemoryProvider.last_init_kwargs = {
            "base_url": base_url,
            "config_path": config_path,
            "config_dict": config_dict,
            "security_provider": security_provider,
            "api_key": api_key,
            "user_id": user_id,
            "agent_id": agent_id,
            "space": space,
            "identity_org": identity_org,
            "identity_user": identity_user,
        }

    @staticmethod
    def is_available() -> bool:
        return _FakeAgentMemoryProvider.available


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    """Fake rail/provider + hermetic env."""
    _FakeJiuwenRail.last_args = None
    _FakeAgentMemoryProvider.last_init_kwargs = None
    _FakeAgentMemoryProvider.available = True

    # The pinned openjiuwen may not export JiuwenMemoryRail at all (the builder
    # then disables the variant). Inject the stub unconditionally so the
    # builder's lazy from-import resolves it in either dependency state.
    monkeypatch.setattr(
        "openjiuwen.harness.rails.JiuwenMemoryRail", _FakeJiuwenRail, raising=False
    )

    # Patch the provider class on the (module-level imported, hence cached)
    # real adapter module — the builder's lazy from-import then resolves the
    # fake without touching the import machinery.
    monkeypatch.setattr(adapter_mod, "AgentMemoryMemoryProvider", _FakeAgentMemoryProvider)

    for env in (
        "JIUWEN_MEMORY_MODE",
        "JIUWEN_MEMORY_BASE_URL",
        "JIUWEN_MEMORY_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)
    yield


def _cfg(jiuwen: dict) -> dict:
    """A full-shaped config with the jiuwen builtin section."""
    return {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": jiuwen}}}


# ---------------------------------------------------------------------------
# mode resolution
# ---------------------------------------------------------------------------

def test_default_mode_is_http():
    assert jrb.get_jiuwen_builtin_mode(_cfg({})) == "http"


def test_sdk_mode_selected_from_config():
    assert jrb.get_jiuwen_builtin_mode(_cfg({"mode": "sdk"})) == "sdk"


def test_sdk_mode_env_override(monkeypatch):
    monkeypatch.setenv("JIUWEN_MEMORY_MODE", "sdk")
    assert jrb.get_jiuwen_builtin_mode(_cfg({})) == "sdk"


def test_unknown_mode_falls_back_to_http():
    assert jrb.get_jiuwen_builtin_mode(_cfg({"mode": "socks"})) == "http"


# ---------------------------------------------------------------------------
# build: http mode (unchanged behavior)
# ---------------------------------------------------------------------------

def test_http_mode_builds_network_provider():
    rail = jrb.build_jiuwen_memory_rail(_cfg({"base_url": "http://127.0.0.1:8137"}))
    assert isinstance(rail, _FakeJiuwenRail)
    init = _FakeAgentMemoryProvider.last_init_kwargs
    assert init["base_url"] == "http://127.0.0.1:8137"
    assert init["config_path"] is None
    assert init["identity_org"] == "local"
    # rail wiring: cross-session sharing default
    assert _FakeJiuwenRail.last_args["session_id"] == ""
    assert _FakeJiuwenRail.last_args["session_end_extract"] is False


# ---------------------------------------------------------------------------
# build: sdk mode
# ---------------------------------------------------------------------------

def test_sdk_bare_uses_provider_builtin_default():
    """sdk + 无内联 sdk dict：provider 自填内置 agent 默认装配。"""
    rail = jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk"}))
    assert isinstance(rail, _FakeJiuwenRail)
    init = _FakeAgentMemoryProvider.last_init_kwargs
    assert init["config_dict"] is None  # provider 端走内置默认
    assert init["config_path"] is None
    assert not init["base_url"]
    assert init["security_provider"] is not None  # mem2.0 进程内身份 fail-closed
    assert _FakeJiuwenRail.last_args["user_id"] == "__default__"
    assert _FakeJiuwenRail.last_args["session_id"] == ""


def test_sdk_inline_config_dict_passthrough():
    """sdk + 内联 sdk dict：整份 dict 直传 provider（config_path 被忽略）。"""
    rail = jrb.build_jiuwen_memory_rail(
        _cfg({"mode": "sdk", "sdk": {"globals": {"llm_model": "inline-model"}}})
    )
    assert isinstance(rail, _FakeJiuwenRail)
    init = _FakeAgentMemoryProvider.last_init_kwargs
    assert init["config_path"] is None
    assert not init["base_url"]
    passed = init["config_dict"]
    assert passed["globals"]["llm_model"] == "inline-model"
    # 空凭证字段就地填入 jiuwenswarm 真源（可能为空串），不破坏内联值
    assert "llm_api_key" in passed["globals"]
    assert "config_path" not in passed


def test_sdk_agent_name_overrides_agent_id():
    """运行 agent name 优先作为 scope.agent 轴。"""
    rail = jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk", "agent_id": "cfg-agent"}), agent_name="run-agent")
    assert isinstance(rail, _FakeJiuwenRail)
    assert _FakeJiuwenRail.last_args["agent_id"] == "run-agent"


# ---------------------------------------------------------------------------
# failure contract: None = memory disabled, never an error
# ---------------------------------------------------------------------------

def test_missing_adapter_class_returns_none(monkeypatch):
    # `from <adapter> import AgentMemoryMemoryProvider` raises ImportError when
    # the attribute is gone — the same builder branch as a missing package.
    monkeypatch.delattr(adapter_mod, "AgentMemoryMemoryProvider")
    assert jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk"})) is None


def test_provider_unavailable_returns_none():
    _FakeAgentMemoryProvider.available = False
    assert jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk"})) is None


# ---------------------------------------------------------------------------
# fingerprint: stable, and sensitive to every semantic change
# ---------------------------------------------------------------------------

def test_fingerprint_stable_for_equal_config():
    cfg = _cfg({"mode": "sdk"})
    assert jrb.jiuwen_builtin_config_fingerprint(cfg) == jrb.jiuwen_builtin_config_fingerprint(cfg)


def test_fingerprint_sensitive_to_mode():
    sdk = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "sdk"}))
    http = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "http", "base_url": "http://x:1"}))
    assert sdk != http


def test_fingerprint_builtin_default_is_constant_marker():
    fp = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "sdk"}))
    assert "builtin-default" in fp


def test_fingerprint_distinguishes_builtin_and_inline_dict():
    bare = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "sdk"}))
    inline_fp = jrb.jiuwen_builtin_config_fingerprint(
        _cfg({"mode": "sdk", "sdk": {"globals": {"a": 1}}})
    )
    assert bare != inline_fp


def test_fingerprint_sensitive_to_inline_dict_content():
    cfg = _cfg({"mode": "sdk", "sdk": {"globals": {"a": 1}}})
    base = jrb.jiuwen_builtin_config_fingerprint(cfg)
    assert jrb.jiuwen_builtin_config_fingerprint(cfg) == base  # stable
    cfg["memory"]["builtin"]["jiuwen"]["sdk"]["globals"]["a"] = 2  # 内容变化
    assert jrb.jiuwen_builtin_config_fingerprint(cfg) != base


def test_fingerprint_sensitive_to_http_endpoint():
    a = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "http", "base_url": "http://a:1"}))
    b = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "http", "base_url": "http://b:1"}))
    assert a != b
