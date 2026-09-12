# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for permission_rail_spec — chat-team leader PermissionInterruptRail provider.

Targets:
- register_permission_rail_provider idempotency
- _build_permission_rail_bundle gating (enabled/disabled, missing config)
- _build_permission_rail_bundle context-driven fields (session_id, project_dir,
  trusted_dirs)
- _build_permission_rail_bundle graceful failure (build_permission_rail raises)
- _resolve_model_name fallback (no config → "default"; with config → config value)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.agents.swarm import permission_rail_spec
from jiuwenswarm.agents.swarm.permission_rail_spec import (
    PERMISSION_RAIL_BUNDLE,
    _build_permission_rail_bundle,
    _resolve_model_name,
    is_permission_rail_provider_registered,
    register_permission_rail_provider,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def reset_registration_flag():
    """Reset the module-level _PROVIDERS_REGISTERED flag around every test.

    The module is imported exactly once per process, but each test should
    see a fresh registration state so registration tests are deterministic.
    """
    saved = permission_rail_spec._PROVIDERS_REGISTERED
    permission_rail_spec._PROVIDERS_REGISTERED = False
    yield
    permission_rail_spec._PROVIDERS_REGISTERED = saved


# ── registration idempotency ────────────────────────────────────────


def test_register_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_permission_rail_provider is safe to call multiple times."""
    captured = []

    def fake_register(name, factory):
        captured.append(name)

    monkeypatch.setattr(
        permission_rail_spec,
        "register_rail_provider",
        fake_register,
    )

    register_permission_rail_provider()
    register_permission_rail_provider()
    register_permission_rail_provider()

    assert len(captured) == 1
    assert captured[0] == PERMISSION_RAIL_BUNDLE
    assert is_permission_rail_provider_registered() is True


def test_register_returns_bool_for_test_consumers() -> None:
    """is_permission_rail_provider_registered reflects the flag state."""
    assert is_permission_rail_provider_registered() is False


def test_register_returns_true_after_first_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        permission_rail_spec, "register_rail_provider", lambda *_a, **_kw: None
    )
    register_permission_rail_provider()
    assert is_permission_rail_provider_registered() is True


# ── factory gating ──────────────────────────────────────────────────


def test_factory_returns_empty_when_params_is_none() -> None:
    """No params at all → empty list, no rail built."""
    result = _build_permission_rail_bundle(None, context=None)  # type: ignore[arg-type]
    assert result == []


def test_factory_returns_empty_when_params_not_dict() -> None:
    """Non-dict params (defensive) → empty list."""
    result = _build_permission_rail_bundle("not-a-dict", context=None)  # type: ignore[arg-type]
    assert result == []


def test_factory_returns_empty_when_permissions_config_missing() -> None:
    """No permissions_config key → empty list."""
    result = _build_permission_rail_bundle({"unrelated": 1}, context=None)
    assert result == []


def test_factory_returns_empty_when_permissions_config_not_dict() -> None:
    """permissions_config present but not a dict → empty list."""
    result = _build_permission_rail_bundle({"permissions_config": "wrong"}, context=None)
    assert result == []


def test_factory_returns_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """permissions.enabled=False → empty list, build_permission_rail never called."""
    build_mock = MagicMock()
    monkeypatch.setattr(permission_rail_spec, "build_permission_rail", build_mock)

    result = _build_permission_rail_bundle(
        {"permissions_config": {"enabled": False, "tools": {"write_file": "ask"}}},
        context=None,
    )

    assert result == []
    build_mock.assert_not_called()


def test_factory_returns_rail_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """permissions.enabled=True → returns [PermissionInterruptRail]."""
    fake_rail = SimpleNamespace(name="fake-rail")
    monkeypatch.setattr(
        permission_rail_spec,
        "build_permission_rail",
        MagicMock(return_value=fake_rail),
    )
    monkeypatch.setattr(
        permission_rail_spec,
        "apply_permission_trusted_dirs",
        MagicMock(),
    )

    result = _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True, "tools": {"write_file": "ask"}}},
        context=None,
    )

    assert result == [fake_rail]


def test_factory_returns_empty_when_build_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_permission_rail returns None → empty list."""
    monkeypatch.setattr(
        permission_rail_spec,
        "build_permission_rail",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        permission_rail_spec,
        "apply_permission_trusted_dirs",
        MagicMock(),
    )

    result = _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=None,
    )

    assert result == []


def test_factory_returns_empty_when_build_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_permission_rail raises → empty list, no propagation."""
    monkeypatch.setattr(
        permission_rail_spec,
        "build_permission_rail",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(
        permission_rail_spec,
        "apply_permission_trusted_dirs",
        MagicMock(),
    )

    result = _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=None,
    )

    assert result == []


# ── factory context-driven fields ──────────────────────────────────


def test_factory_uses_session_id_from_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """context.session_id is forwarded to build_permission_rail."""
    fake_rail = SimpleNamespace(name="fake-rail")
    build_mock = MagicMock(return_value=fake_rail)
    monkeypatch.setattr(permission_rail_spec, "build_permission_rail", build_mock)
    monkeypatch.setattr(permission_rail_spec, "apply_permission_trusted_dirs", MagicMock())
    monkeypatch.setattr(permission_rail_spec, "_resolve_model_name", lambda: "fixed-model")

    context = SimpleNamespace(session_id="ctx-session-id-42", project_dir=None, trusted_dirs=None)
    _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=context,
    )

    assert build_mock.call_count == 1
    kwargs = build_mock.call_args.kwargs
    assert kwargs["session_id"] == "ctx-session-id-42"
    assert kwargs["model_name"] == "fixed-model"
    assert kwargs["llm"] is None


def test_factory_falls_back_session_id_when_context_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No context → default 'chat-team-leader' session_id."""
    fake_rail = SimpleNamespace(name="fake-rail")
    build_mock = MagicMock(return_value=fake_rail)
    monkeypatch.setattr(permission_rail_spec, "build_permission_rail", build_mock)
    monkeypatch.setattr(permission_rail_spec, "apply_permission_trusted_dirs", MagicMock())

    _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=None,
    )

    kwargs = build_mock.call_args.kwargs
    assert kwargs["session_id"] == "chat-team-leader"


def test_factory_falls_back_session_id_when_context_session_id_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """context.session_id is empty string → default sentinel."""
    fake_rail = SimpleNamespace(name="fake-rail")
    build_mock = MagicMock(return_value=fake_rail)
    monkeypatch.setattr(permission_rail_spec, "build_permission_rail", build_mock)
    monkeypatch.setattr(permission_rail_spec, "apply_permission_trusted_dirs", MagicMock())

    context = SimpleNamespace(session_id="", project_dir=None, trusted_dirs=None)
    _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=context,
    )

    kwargs = build_mock.call_args.kwargs
    assert kwargs["session_id"] == "chat-team-leader"


def test_factory_applies_trusted_dirs_when_context_provides_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """project_dir or trusted_dirs present → apply_permission_trusted_dirs is called."""
    fake_rail = SimpleNamespace(name="fake-rail")
    build_mock = MagicMock(return_value=fake_rail)
    trusted_mock = MagicMock()
    monkeypatch.setattr(permission_rail_spec, "build_permission_rail", build_mock)
    monkeypatch.setattr(permission_rail_spec, "apply_permission_trusted_dirs", trusted_mock)

    context = SimpleNamespace(
        session_id="s1",
        project_dir="/some/project",
        trusted_dirs=["/some/project", "/tmp/safe"],
    )
    _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=context,
    )

    trusted_mock.assert_called_once()
    kwargs = trusted_mock.call_args.kwargs
    assert kwargs["trusted_dirs"] == ["/some/project", "/tmp/safe"]
    assert kwargs["project_dir"] == "/some/project"


def test_factory_skips_trusted_dirs_when_context_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither project_dir nor trusted_dirs set → apply_permission_trusted_dirs skipped."""
    fake_rail = SimpleNamespace(name="fake-rail")
    build_mock = MagicMock(return_value=fake_rail)
    trusted_mock = MagicMock()
    monkeypatch.setattr(permission_rail_spec, "build_permission_rail", build_mock)
    monkeypatch.setattr(permission_rail_spec, "apply_permission_trusted_dirs", trusted_mock)

    context = SimpleNamespace(session_id="s1", project_dir=None, trusted_dirs=None)
    _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=context,
    )

    trusted_mock.assert_not_called()


def test_factory_continues_when_apply_trusted_dirs_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_permission_trusted_dirs exception is swallowed, rail still returned."""
    fake_rail = SimpleNamespace(name="fake-rail")
    monkeypatch.setattr(
        permission_rail_spec,
        "build_permission_rail",
        MagicMock(return_value=fake_rail),
    )
    monkeypatch.setattr(
        permission_rail_spec,
        "apply_permission_trusted_dirs",
        MagicMock(side_effect=RuntimeError("trusted dirs bug")),
    )

    context = SimpleNamespace(
        session_id="s1",
        project_dir="/p",
        trusted_dirs=["/p"],
    )
    result = _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=context,
    )

    assert result == [fake_rail]


def test_factory_handles_context_without_session_id_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """context has no session_id attribute (older BuildContext) → falls back."""
    fake_rail = SimpleNamespace(name="fake-rail")
    build_mock = MagicMock(return_value=fake_rail)
    monkeypatch.setattr(permission_rail_spec, "build_permission_rail", build_mock)
    monkeypatch.setattr(permission_rail_spec, "apply_permission_trusted_dirs", MagicMock())

    context = SimpleNamespace()  # no attributes at all
    _build_permission_rail_bundle(
        {"permissions_config": {"enabled": True}},
        context=context,
    )

    kwargs = build_mock.call_args.kwargs
    assert kwargs["session_id"] == "chat-team-leader"


def test_factory_passes_permissions_config_under_permissions_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """permissions_config is wrapped under 'permissions' key when forwarded."""
    fake_rail = SimpleNamespace(name="fake-rail")
    build_mock = MagicMock(return_value=fake_rail)
    monkeypatch.setattr(permission_rail_spec, "build_permission_rail", build_mock)
    monkeypatch.setattr(permission_rail_spec, "apply_permission_trusted_dirs", MagicMock())

    pc = {"enabled": True, "tools": {"write_file": "ask"}, "schema": "tiered_policy"}
    _build_permission_rail_bundle(
        {"permissions_config": pc},
        context=None,
    )

    forwarded_config = build_mock.call_args.kwargs["config"]
    assert forwarded_config == {"permissions": pc}


# ── _resolve_model_name ─────────────────────────────────────────────


def test_resolve_model_name_falls_back_to_default_when_get_config_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_config() blows up → returns 'default' sentinel, no propagation."""

    def boom():
        raise RuntimeError("config broken")

    monkeypatch.setattr(permission_rail_spec, "get_config", boom)
    assert _resolve_model_name() == "default"


def test_resolve_model_name_falls_back_when_config_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(permission_rail_spec, "get_config", lambda: {})
    assert _resolve_model_name() == "default"


def test_resolve_model_name_falls_back_when_models_section_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(permission_rail_spec, "get_config", lambda: {"models": {}})
    assert _resolve_model_name() == "default"


def test_resolve_model_name_falls_back_when_default_model_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        permission_rail_spec,
        "get_config",
        lambda: {"models": {"default": {}}},
    )
    assert _resolve_model_name() == "default"


def test_resolve_model_name_falls_back_when_model_client_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        permission_rail_spec,
        "get_config",
        lambda: {"models": {"default": {"model_client_config": {}}}},
    )
    assert _resolve_model_name() == "default"


def test_resolve_model_name_reads_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        permission_rail_spec,
        "get_config",
        lambda: {
            "models": {
                "default": {"model_client_config": {"model_name": "gpt-4o"}},
            },
        },
    )
    assert _resolve_model_name() == "gpt-4o"


def test_resolve_model_name_handles_non_dict_models_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: models section is a string → fall back."""
    monkeypatch.setattr(
        permission_rail_spec,
        "get_config",
        lambda: {"models": "not-a-dict"},
    )
    assert _resolve_model_name() == "default"


# ── registered factory integration ─────────────────────────────────


def test_registered_factory_is_callable_with_expected_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory registered with the SDK has the (params, context) signature."""

    captured = {}

    def fake_register(name, factory):
        captured["name"] = name
        captured["factory"] = factory

    monkeypatch.setattr(
        permission_rail_spec,
        "register_rail_provider",
        fake_register,
    )
    register_permission_rail_provider()

    assert captured["name"] == PERMISSION_RAIL_BUNDLE
    assert captured["factory"] is _build_permission_rail_bundle
