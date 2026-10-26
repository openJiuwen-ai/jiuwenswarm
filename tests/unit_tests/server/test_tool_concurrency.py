# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from jiuwenswarm.server.tool_concurrency import resolve_concurrency_policy


def test_resolve_concurrency_policy_via_config_provider():
    policy = resolve_concurrency_policy(
        config_provider=lambda: {
            "react": {
                "concurrency": {
                    "enabled": True,
                    "tool_limits": {"web_search": 4},
                }
            }
        }
    )
    assert policy.tools["web_search"].limit == 4


def test_resolve_policy_reads_concurrency_tool_limits(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.common.config",
        SimpleNamespace(
            get_config=lambda: {
                "react": {
                    "concurrency": {
                        "enabled": True,
                        "tool_limits": {
                            "web_search": {"limit": 2},
                            "bash": 5,
                        },
                    }
                }
            }
        ),
    )
    policy = resolve_concurrency_policy()
    assert policy.tools["web_search"].limit == 2
    assert policy.tools["bash"].limit == 5


def test_resolve_concurrency_policy_integer_shorthand(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.common.config",
        SimpleNamespace(
            get_config=lambda: {
                "react": {
                    "concurrency": {
                        "enabled": True,
                        "tool_limits": {"web_search": 3},
                    }
                }
            }
        ),
    )
    policy = resolve_concurrency_policy()
    assert policy.tools["web_search"].limit == 3


def test_resolve_concurrency_policy_disabled(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.common.config",
        SimpleNamespace(
            get_config=lambda: {
                "react": {
                    "concurrency": {
                        "enabled": False,
                        "tool_limits": {"web_search": 5},
                    }
                }
            }
        ),
    )
    policy = resolve_concurrency_policy()
    assert policy.enabled is False
    assert policy.tools == {}


def test_register_empty_policy_still_wires_controller(monkeypatch):
    """Empty policy must still register so later hot-reload can activate limits."""
    import sys
    from types import SimpleNamespace

    from jiuwenswarm.server import tool_concurrency as tc

    monkeypatch.setitem(
        sys.modules,
        "jiuwenswarm.common.config",
        SimpleNamespace(get_config=lambda: {"react": {}}),
    )
    monkeypatch.setattr(tc, "_controller", None)

    captured: dict = {}

    class _FakeAbilityManager:
        @classmethod
        def configure_tool_batch_concurrency(cls, controller):
            captured["controller"] = controller

    monkeypatch.setitem(
        sys.modules,
        "openjiuwen.core.single_agent.ability_manager",
        SimpleNamespace(AbilityManager=_FakeAbilityManager),
    )
    monkeypatch.setitem(
        sys.modules,
        "openjiuwen.core.single_agent.tool_batch_concurrency",
        SimpleNamespace(
            ToolBatchConcurrencyController=lambda policy_provider: SimpleNamespace(
                policy_provider=policy_provider
            ),
            ToolBatchConcurrencyPolicy=object,
            ToolConcurrencyRule=object,
        ),
    )

    tc.register_tool_batch_concurrency()
    assert captured.get("controller") is not None
