"""Unit tests for OpenCode Go vendor identity and request-header helpers."""

from __future__ import annotations

import re

from agent_gateway.common.model_selection import (
    ResolvedModel,
    ResolvedModelGroup,
    ResolvedRoute,
)

from jiuwenswarm.common.opencode_go import (
    OPENCODE_GO_SESSION_HEADER,
    OPENCODE_GO_VENDOR_KEY,
    SCOPE_AUX,
    SCOPE_PROBE,
    SCOPE_UNSCOPED,
    derive_session_id,
    inject_session_header,
    inject_session_into_selection,
    is_opencode_go_client_config,
    resolve_scope,
)

HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _resolved_model(model_id: str = "mdl_1", **overrides) -> ResolvedModel:
    data = {
        "model_id": model_id,
        "source": "defaults",
        "model_name": "deepseek-v4.1-flash",
        "provider": "OpenAI",
        "api_base": "https://opencode.ai/zen/go/v1",
        "api_key": "sk-test-not-a-real-key",
        "client_options": {"vendor_key": OPENCODE_GO_VENDOR_KEY},
    }
    data.update(overrides)
    return ResolvedModel(**data)


def test_vendor_constants() -> None:
    assert OPENCODE_GO_VENDOR_KEY == "opencode-go"
    assert OPENCODE_GO_SESSION_HEADER == "x-opencode-session"
    assert SCOPE_PROBE == "probe:config-validate"
    assert SCOPE_UNSCOPED == "session:unscoped"


def test_identity_requires_vendor_key_only() -> None:
    assert is_opencode_go_client_config({"vendor_key": "opencode-go"})
    assert is_opencode_go_client_config({"vendor_key": " OpenCode-Go "})
    assert not is_opencode_go_client_config(
        {"api_base": "https://opencode.ai/zen/go/v1"}
    )
    assert not is_opencode_go_client_config({"vendor_key": "openai"})
    assert not is_opencode_go_client_config({})
    assert not is_opencode_go_client_config(None)


def test_derive_session_id_is_stable_scoped_and_opaque() -> None:
    first = derive_session_id("session:conv-1")
    second = derive_session_id("session:conv-1")
    other = derive_session_id("session:conv-2")

    assert first == second
    assert first != other
    assert HEX32.fullmatch(first)


def test_inject_session_header_adds_derived_value() -> None:
    mcc = {
        "vendor_key": "opencode-go",
        "api_base": "https://opencode.ai/zen/go/v1",
        "custom_headers": {"X-Trace": "trace-1"},
    }

    result = inject_session_header(mcc, "session:conv-1")

    assert result is mcc
    assert result["custom_headers"]["X-Trace"] == "trace-1"
    assert (
        result["custom_headers"][OPENCODE_GO_SESSION_HEADER]
        == derive_session_id("session:conv-1")
    )


def test_inject_session_header_keeps_user_value_without_duplicate() -> None:
    mcc = {
        "vendor_key": "opencode-go",
        "custom_headers": {"X-OpenCode-Session": "user-value"},
    }

    inject_session_header(mcc, "session:conv-1")

    matching = [
        key
        for key in mcc["custom_headers"]
        if key.lower() == OPENCODE_GO_SESSION_HEADER
    ]
    assert matching == ["X-OpenCode-Session"]
    assert mcc["custom_headers"]["X-OpenCode-Session"] == "user-value"


def test_inject_session_header_is_noop_for_other_vendors() -> None:
    mcc = {"client_provider": "OpenAI", "api_base": "https://opencode.ai/zen/go/v1"}

    result = inject_session_header(mcc, "session:conv-1")

    assert result is mcc
    assert "custom_headers" not in mcc


def test_inject_session_into_model_returns_copy() -> None:
    resolved = _resolved_model()

    injected = inject_session_into_selection(resolved, "session:conv-1")

    assert injected is not resolved
    assert injected.client_options["custom_headers"][
        OPENCODE_GO_SESSION_HEADER
    ] == derive_session_id("session:conv-1")
    assert "custom_headers" not in resolved.client_options


def test_inject_session_into_group_injects_every_route() -> None:
    group = ResolvedModelGroup(
        model_group_id="mgp_1",
        routes=[
            ResolvedRoute(route_id="r1", model=_resolved_model()),
            ResolvedRoute(route_id="r2", model=_resolved_model("mdl_2")),
        ],
    )

    injected = inject_session_into_selection(group, "session:conv-1")

    expected = derive_session_id("session:conv-1")
    assert injected is not group
    assert all(
        route.model.client_options["custom_headers"][OPENCODE_GO_SESSION_HEADER]
        == expected
        for route in injected.routes
    )
    assert all(
        "custom_headers" not in route.model.client_options
        for route in group.routes
    )


def test_resolve_scope_uses_session_or_constant() -> None:
    assert resolve_scope("conv-1") == "session:conv-1"
    assert resolve_scope("  conv-1  ") == "session:conv-1"
    assert resolve_scope(None) == SCOPE_UNSCOPED
    assert resolve_scope("") == SCOPE_UNSCOPED


class TestBuildModelFromEntryAuxFallback:
    """Shared model builders (non-recipe paths) get a fixed aux session header."""

    def _mcc(self, **extra) -> dict:
        mcc = {
            "model_name": "oc-model",
            "api_base": "https://opencode.ai/zen/go/v1",
            "api_key": "sk-test-not-a-real-key",
            "client_provider": "OpenAI",
            "vendor_key": OPENCODE_GO_VENDOR_KEY,
        }
        mcc.update(extra)
        return mcc

    def test_opencode_entry_gets_fixed_aux_scope(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            build_model_from_entry,
        )

        model = build_model_from_entry(self._mcc(), {})

        assert model.model_client_config.custom_headers == {
            OPENCODE_GO_SESSION_HEADER: derive_session_id(SCOPE_AUX)
        }

    def test_existing_session_header_is_not_overwritten(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            build_model_from_entry,
        )

        model = build_model_from_entry(
            self._mcc(custom_headers={"X-OpenCode-Session": "user-value"}),
            {},
        )

        assert model.model_client_config.custom_headers == {
            "X-OpenCode-Session": "user-value"
        }

    def test_input_mcc_is_not_mutated(self) -> None:
        from copy import deepcopy

        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            build_model_from_entry,
        )

        mcc = self._mcc()
        snapshot = deepcopy(mcc)

        build_model_from_entry(mcc, {})

        assert mcc == snapshot

    def test_other_vendors_are_unchanged(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            build_model_from_entry,
        )

        model = build_model_from_entry(
            self._mcc(vendor_key="deepseek", custom_headers={"X-Trace": "1"}),
            {},
        )

        assert model.model_client_config.custom_headers == {"X-Trace": "1"}


def test_inject_session_into_unknown_selection_shape_passes_through() -> None:
    sentinel = object()

    assert inject_session_into_selection(sentinel, "session:conv-1") is sentinel


class TestLegacyConfigCompatibility:
    """Legacy hand-written entries keep working and are never rewritten."""

    def test_legacy_entry_without_vendor_key_is_untouched(self) -> None:
        from copy import deepcopy

        mcc = {
            "model_name": "legacy-model",
            "api_base": "https://opencode.ai/zen/go/v1",
            "api_key": "sk-test-not-a-real-key",
            "custom_headers": {"X-OpenCode-Session": "manual-value"},
        }
        snapshot = deepcopy(mcc)

        injected = inject_session_header(mcc, "session:conv-1")

        assert injected == snapshot
        assert mcc == snapshot
        assert "vendor_key" not in injected

    def test_legacy_entry_keeps_the_manual_header_at_runtime(self) -> None:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            build_model_from_entry,
        )

        model = build_model_from_entry(
            {
                "model_name": "legacy-model",
                "api_base": "https://opencode.ai/zen/go/v1",
                "api_key": "sk-test-not-a-real-key",
                "custom_headers": {"X-OpenCode-Session": "manual-value"},
            },
            {},
        )

        assert model.model_client_config.custom_headers == {
            "X-OpenCode-Session": "manual-value"
        }
        assert getattr(model.model_client_config, "vendor_key", None) in (None, "")
