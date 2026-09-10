# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for model selection/switch feature.

Covers:
- _validate_top_level_models
- validate_sync_payload models passthrough
- TenantAgentPool._build_service_model_cache (alias-keyed) / _service_model_cache lifecycle
- _resolve_model_for_request logic (shared cache alias lookup + ValueError)
- _resolve_from_shared_model_cache logic
- _merge_service_model_cache logic
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from jiuwenclaw.agentserver.sync_agents_configs import (
    SYNC_ENV_SCHEMA,
    _validate_top_level_models,
    validate_sync_payload,
)
from jiuwenclaw.agentserver.tenant_agent_pool import TenantAgentPool
from jiuwenclaw.agentserver.tenant_catalog_registry import TenantCatalogRegistry
from jiuwenclaw.local_env_config import reset_local_env_state_for_tests


def _full_env(**overrides: str | None) -> dict[str, str | None]:
    base: dict[str, str | None] = {key: "" for key in SYNC_ENV_SCHEMA}
    base.update(overrides)
    return base


def _sync_payload(
    *,
    revision: str = "rev-1",
    service_id: str = "default",
    agents: list[dict] | None = None,
    models: list[dict] | None = None,
) -> dict:
    if agents is None:
        agents = [
            {
                "agent_id": "office",
                "config": {"react": {"agent_name": "office"}},
                "env": _full_env(MODEL_NAME="office-model"),
                "runtime": {},
            }
        ]
    payload = {
        "revision": revision,
        "service_id": service_id,
        "agents": agents,
    }
    if models is not None:
        payload["models"] = models
    return payload


def _model_entry(
    model_name: str = "model-a",
    alias: str = "",
    api_base: str = "https://api.example.com",
    api_key: str = "sk-test",
    client_provider: str = "OpenAI",
    **extra_mcc: object,
) -> dict:
    mcc: dict = {
        "model_name": model_name,
        "api_base": api_base,
        "api_key": api_key,
        "client_provider": client_provider,
    }
    mcc.update(extra_mcc)
    entry: dict = {"model_client_config": mcc}
    if alias:
        entry["alias"] = alias
    return entry


def _make_stub_adapter(
    model_cache: dict | None = None,
    env_service_id: str = "test-svc",
    default_model: MagicMock | None = None,
    default_baseline_model: MagicMock | None = None,
):
    """Create a stub adapter with _resolve_model_for_request and related methods.

    NOTE: This copies the production logic from interface_deep.py rather than
    importing JiuWenClawDeepAdapter (which pulls in the openjiuwen import chain).
    If the production code changes, these tests will NOT automatically fail.
    Re-sync this stub when the production logic is updated.
    """
    adapter = MagicMock()
    adapter._model_cache = model_cache if model_cache is not None else {}
    adapter._env_service_id = env_service_id
    adapter._model = default_model if default_model is not None else MagicMock()
    # Mirror production: _default_model is the sync-reload baseline; switch_model
    # does NOT touch it. None here means "fall back to _model" (cold start before
    # any sync reload has run).
    adapter._default_model = default_baseline_model

    def _resolve_model_for_request(request):
        params = request.params if isinstance(request.params, dict) else {}
        requested = str(params.get("model_name") or "").strip()
        if not requested:
            # Mirror production: return _default_model (sync-reload baseline)
            # so a prior switch_model(self._model override) does not linger.
            return adapter._default_model or adapter._model
        if requested in adapter._model_cache:
            return adapter._model_cache[requested]
        resolved = adapter._resolve_from_shared_model_cache(requested)
        if resolved is not None:
            return resolved
        available = sorted(adapter._model_cache.keys())
        raise ValueError(
            f"model_name {requested!r} not found in service model cache; "
            f"available: {available}"
        )

    def _resolve_from_shared_model_cache(name):
        pool = TenantAgentPool.peek_instance()
        if pool is None:
            return None
        service_cache = pool._service_model_cache.get(adapter._env_service_id, {})
        entry = service_cache.get(name)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            return None
        mcc = dict(entry.get("model_client_config") or {})
        if not str(mcc.get("model_name") or "").strip():
            return None
        mco = entry.get("model_config_obj") or {}
        try:
            model = adapter._build_model_from_entry(mcc, mco)
        except Exception:
            return None
        adapter._model_cache[name] = model
        return model

    def _merge_service_model_cache():
        pool = TenantAgentPool.peek_instance()
        if pool is None:
            return
        service_cache = pool._service_model_cache.get(adapter._env_service_id, {})
        if not service_cache:
            return
        for key, entry in service_cache.items():
            if key in adapter._model_cache:
                continue
            if not isinstance(entry, dict):
                continue
            mcc = dict(entry.get("model_client_config") or {})
            if not str(mcc.get("model_name") or "").strip():
                continue
            mco = entry.get("model_config_obj") or {}
            try:
                adapter._model_cache[key] = adapter._build_model_from_entry(mcc, mco)
            except Exception:
                pass

    adapter._resolve_model_for_request = _resolve_model_for_request
    adapter._resolve_from_shared_model_cache = _resolve_from_shared_model_cache
    adapter._merge_service_model_cache = _merge_service_model_cache
    adapter._build_model_from_entry = MagicMock(return_value=MagicMock())
    return adapter


@pytest.fixture(autouse=True)
def _reset_state():
    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    TenantCatalogRegistry.reset_for_tests()
    TenantAgentPool.reset_instance()
    yield
    reset_local_env_state_for_tests()
    TenantCatalogRegistry.reset_for_tests()
    TenantAgentPool.reset_instance()
    os.environ.clear()
    os.environ.update(saved)


# ---------------------------------------------------------------------------
# 1. _validate_top_level_models
# ---------------------------------------------------------------------------


class TestValidateTopLevelModels:
    @staticmethod
    def test_valid_single_entry():
        result = _validate_top_level_models([_model_entry("model-a")])
        assert len(result) == 1
        assert result[0]["model_client_config"]["model_name"] == "model-a"

    @staticmethod
    def test_valid_multiple_entries_with_alias():
        result = _validate_top_level_models([
            _model_entry("model-a", alias="a"),
            _model_entry("model-b", alias="b"),
        ])
        assert len(result) == 2

    @staticmethod
    def test_rejects_non_array():
        with pytest.raises(ValueError, match="must be an array"):
            _validate_top_level_models("not-a-list")

    @staticmethod
    def test_rejects_non_object_entry():
        with pytest.raises(ValueError, match=r"models\[0\] must be an object"):
            _validate_top_level_models(["not-an-object"])

    @staticmethod
    def test_rejects_missing_model_client_config():
        with pytest.raises(ValueError, match="model_client_config is required"):
            _validate_top_level_models([{"alias": "x"}])

    @staticmethod
    def test_rejects_non_dict_model_client_config():
        with pytest.raises(ValueError, match="model_client_config is required"):
            _validate_top_level_models([{"model_client_config": "bad"}])

    @staticmethod
    def test_rejects_empty_model_name():
        entry = _model_entry("")
        entry["model_client_config"]["model_name"] = ""
        with pytest.raises(ValueError, match="model_name is required"):
            _validate_top_level_models([entry])

    @staticmethod
    def test_rejects_whitespace_model_name():
        entry = _model_entry("  ")
        entry["model_client_config"]["model_name"] = "   "
        with pytest.raises(ValueError, match="model_name is required"):
            _validate_top_level_models([entry])

    @staticmethod
    def test_rejects_duplicate_model_name():
        with pytest.raises(ValueError, match="duplicate"):
            _validate_top_level_models([
                _model_entry("model-a"),
                _model_entry("model-a"),
            ])

    @staticmethod
    def test_rejects_alias_matching_other_entry_model_name():
        with pytest.raises(ValueError, match="duplicate"):
            _validate_top_level_models([
                _model_entry("model-a", alias="model-b"),
                _model_entry("model-b"),
            ])

    @staticmethod
    def test_accepts_alias_same_as_own_model_name():
        result = _validate_top_level_models([
            _model_entry("model-a", alias="model-a"),
        ])
        assert len(result) == 1

    @staticmethod
    def test_rejects_duplicate_alias():
        with pytest.raises(ValueError, match="duplicate alias"):
            _validate_top_level_models([
                _model_entry("model-a", alias="same-alias"),
                _model_entry("model-b", alias="same-alias"),
            ])

    @staticmethod
    def test_accepts_empty_alias():
        result = _validate_top_level_models([_model_entry("model-a", alias="")])
        assert len(result) == 1

    @staticmethod
    def test_accepts_no_alias_key():
        entry = _model_entry("model-a")
        entry.pop("alias", None)
        result = _validate_top_level_models([entry])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 2. validate_sync_payload models passthrough
# ---------------------------------------------------------------------------


class TestValidateSyncPayloadModels:
    @staticmethod
    def test_models_absent_returns_none():
        result = validate_sync_payload(_sync_payload())
        assert result.get("models") is None

    @staticmethod
    def test_models_none_returns_none():
        result = validate_sync_payload(_sync_payload(models=None))
        assert result.get("models") is None

    @staticmethod
    def test_models_valid_passed_through():
        models = [_model_entry("model-a"), _model_entry("model-b")]
        result = validate_sync_payload(_sync_payload(models=models))
        assert result["models"] is not None
        assert len(result["models"]) == 2

    @staticmethod
    def test_models_invalid_raises():
        with pytest.raises(ValueError, match="must be an array"):
            validate_sync_payload(_sync_payload(models="bad"))

    @staticmethod
    def test_models_empty_array_accepted():
        result = validate_sync_payload(_sync_payload(models=[]))
        assert result["models"] == []


# ---------------------------------------------------------------------------
# 3. TenantAgentPool._build_service_model_cache
# ---------------------------------------------------------------------------


class TestBuildServiceModelCache:
    @staticmethod
    def test_no_alias_keyed_by_model_name():
        pool = TenantAgentPool.get_instance()
        entries = [_model_entry("model-a"), _model_entry("model-b")]
        cache = pool._build_service_model_cache("svc-1", entries)
        assert "model-a" in cache
        assert "model-b" in cache
        assert cache["model-a"]["model_client_config"]["model_name"] == "model-a"

    @staticmethod
    def test_with_alias_keyed_by_alias():
        pool = TenantAgentPool.get_instance()
        entries = [_model_entry("model-a", alias="a")]
        cache = pool._build_service_model_cache("svc-1", entries)
        assert "a" in cache
        assert "model-a" not in cache
        assert cache["a"]["model_client_config"]["model_name"] == "model-a"

    @staticmethod
    def test_mixed_alias_and_no_alias():
        pool = TenantAgentPool.get_instance()
        entries = [
            _model_entry("model-a", alias="a"),
            _model_entry("model-b"),
        ]
        cache = pool._build_service_model_cache("svc-1", entries)
        assert "a" in cache
        assert "model-a" not in cache
        assert "model-b" in cache

    @staticmethod
    def test_alias_does_not_overwrite_previous_entry():
        pool = TenantAgentPool.get_instance()
        entries = [
            _model_entry("model-x", alias="a"),
            _model_entry("model-y", alias="a"),
        ]
        cache = pool._build_service_model_cache("svc-1", entries)
        assert "a" in cache
        assert cache["a"]["model_client_config"]["model_name"] == "model-y"

    @staticmethod
    def test_skips_empty_model_name():
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("")
        entry["model_client_config"]["model_name"] = ""
        cache = pool._build_service_model_cache("svc-1", [entry])
        assert len(cache) == 0

    @staticmethod
    def test_stored_in_service_model_cache():
        pool = TenantAgentPool.get_instance()
        entries = [_model_entry("model-a")]
        pool._build_service_model_cache("svc-1", entries)
        assert "svc-1" in pool._service_model_cache
        assert "model-a" in pool._service_model_cache["svc-1"]

    @staticmethod
    def test_different_services_isolated():
        pool = TenantAgentPool.get_instance()
        pool._build_service_model_cache("svc-1", [_model_entry("model-a")])
        pool._build_service_model_cache("svc-2", [_model_entry("model-b")])
        assert "model-a" in pool._service_model_cache["svc-1"]
        assert "model-b" not in pool._service_model_cache["svc-1"]
        assert "model-b" in pool._service_model_cache["svc-2"]
        assert "model-a" not in pool._service_model_cache["svc-2"]


# ---------------------------------------------------------------------------
# 4. TenantAgentPool._service_model_cache lifecycle via sync
# ---------------------------------------------------------------------------


class TestServiceModelCacheLifecycle:
    @staticmethod
    @pytest.mark.asyncio
    async def test_sync_builds_cache_when_models_provided():
        pool = TenantAgentPool.get_instance()
        with patch.object(
            TenantAgentPool, "_ensure_agent_manager",
            new=MagicMock(),
        ):
            await pool.sync_agents_configs(
                _sync_payload(
                    revision="rev-1",
                    models=[_model_entry("model-a", alias="a")],
                )
            )
        assert "default" in pool._service_model_cache
        assert "a" in pool._service_model_cache["default"]
        assert "model-a" not in pool._service_model_cache["default"]

    @staticmethod
    @pytest.mark.asyncio
    async def test_sync_builds_cache_no_alias():
        pool = TenantAgentPool.get_instance()
        with patch.object(
            TenantAgentPool, "_ensure_agent_manager",
            new=MagicMock(),
        ):
            await pool.sync_agents_configs(
                _sync_payload(
                    revision="rev-1",
                    models=[_model_entry("model-a")],
                )
            )
        assert "default" in pool._service_model_cache
        assert "model-a" in pool._service_model_cache["default"]

    @staticmethod
    @pytest.mark.asyncio
    async def test_sync_clears_cache_when_models_absent():
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache["default"] = {"model-a": {}}
        with patch.object(
            TenantAgentPool, "_ensure_agent_manager",
            new=MagicMock(),
        ):
            await pool.sync_agents_configs(
                _sync_payload(revision="rev-2")
            )
        assert "default" not in pool._service_model_cache

    @staticmethod
    @pytest.mark.asyncio
    async def test_sync_clears_cache_when_models_null():
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache["default"] = {"model-a": {}}
        with patch.object(
            TenantAgentPool, "_ensure_agent_manager",
            new=MagicMock(),
        ):
            await pool.sync_agents_configs(
                _sync_payload(revision="rev-2", models=None)
            )
        assert "default" not in pool._service_model_cache

    @staticmethod
    def test_reset_instance_clears_service_model_cache():
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache["svc-1"] = {"model-a": {}}
        TenantAgentPool.reset_instance()
        pool2 = TenantAgentPool.get_instance()
        assert len(pool2._service_model_cache) == 0


# ---------------------------------------------------------------------------
# 5. _resolve_model_for_request logic
# ---------------------------------------------------------------------------


class TestResolveModelForRequest:
    def test_no_model_name_returns_default(self):
        baseline = MagicMock(name="baseline")
        adapter = _make_stub_adapter(default_baseline_model=baseline)
        req = MagicMock()
        req.params = {}
        result = adapter._resolve_model_for_request(req)
        assert result is baseline

    def test_empty_model_name_returns_default(self):
        baseline = MagicMock(name="baseline")
        adapter = _make_stub_adapter(default_baseline_model=baseline)
        req = MagicMock()
        req.params = {"model_name": ""}
        result = adapter._resolve_model_for_request(req)
        assert result is baseline

    def test_whitespace_model_name_returns_default(self):
        baseline = MagicMock(name="baseline")
        adapter = _make_stub_adapter(default_baseline_model=baseline)
        req = MagicMock()
        req.params = {"model_name": "   "}
        result = adapter._resolve_model_for_request(req)
        assert result is baseline

    def test_non_dict_params_returns_default(self):
        baseline = MagicMock(name="baseline")
        adapter = _make_stub_adapter(default_baseline_model=baseline)
        req = MagicMock()
        req.params = "not-a-dict"
        result = adapter._resolve_model_for_request(req)
        assert result is baseline

    def test_no_model_name_falls_back_to_model_when_default_none(self):
        # Cold start: _default_model is None (no sync reload yet) → fall back
        # to _model so the adapter still has a usable model.
        current = MagicMock(name="current")
        adapter = _make_stub_adapter(default_model=current, default_baseline_model=None)
        req = MagicMock()
        req.params = {}
        result = adapter._resolve_model_for_request(req)
        assert result is current

    def test_no_model_name_returns_default_not_current_after_switch(self):
        # switch_model changed _model to B, but _default_model stays A.
        # Not passing model_name must return A (default), not B (switched).
        baseline = MagicMock(name="baseline-A")
        switched = MagicMock(name="switched-B")
        adapter = _make_stub_adapter(default_model=switched, default_baseline_model=baseline)
        req = MagicMock()
        req.params = {}
        result = adapter._resolve_model_for_request(req)
        assert result is baseline
        assert result is not switched

    def test_model_name_in_cache_returns_cached(self):
        mock_model = MagicMock()
        adapter = _make_stub_adapter(model_cache={"model-a": mock_model})
        req = MagicMock()
        req.params = {"model_name": "model-a"}
        result = adapter._resolve_model_for_request(req)
        assert result is mock_model

    def test_alias_in_cache_returns_cached(self):
        mock_model = MagicMock()
        adapter = _make_stub_adapter(model_cache={"a": mock_model})
        req = MagicMock()
        req.params = {"model_name": "a"}
        result = adapter._resolve_model_for_request(req)
        assert result is mock_model

    def test_unknown_model_name_raises_value_error(self):
        adapter = _make_stub_adapter(model_cache={"model-a": MagicMock()})
        req = MagicMock()
        req.params = {"model_name": "nonexistent"}
        with pytest.raises(ValueError, match="model_name.*not found"):
            adapter._resolve_model_for_request(req)

    def test_value_error_includes_available_models(self):
        adapter = _make_stub_adapter(
            model_cache={"model-a": MagicMock(), "model-b": MagicMock()},
        )
        req = MagicMock()
        req.params = {"model_name": "nonexistent"}
        with pytest.raises(ValueError, match="model-a") as exc_info:
            adapter._resolve_model_for_request(req)
        assert "model-b" in str(exc_info.value)

    def test_resolve_from_shared_cache_fallback(self):
        adapter = _make_stub_adapter()
        mock_model = MagicMock()
        adapter._resolve_from_shared_model_cache = MagicMock(return_value=mock_model)
        req = MagicMock()
        req.params = {"model_name": "a"}
        result = adapter._resolve_model_for_request(req)
        assert result is mock_model

    def test_resolve_from_shared_cache_returns_none_raises(self):
        adapter = _make_stub_adapter(model_cache={"model-a": MagicMock()})
        adapter._resolve_from_shared_model_cache = MagicMock(return_value=None)
        req = MagicMock()
        req.params = {"model_name": "nonexistent"}
        with pytest.raises(ValueError, match="not found"):
            adapter._resolve_model_for_request(req)

    def test_model_name_in_shared_cache_resolves_by_alias(self):
        adapter = _make_stub_adapter(model_cache={})
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("gpt-4o", alias="fast", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"fast": entry}
        mock_model = MagicMock()
        adapter._build_model_from_entry = MagicMock(return_value=mock_model)
        req = MagicMock()
        req.params = {"model_name": "fast"}
        result = adapter._resolve_model_for_request(req)
        assert result is mock_model

    def test_model_name_in_shared_cache_no_alias(self):
        adapter = _make_stub_adapter(model_cache={})
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("model-a", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"model-a": entry}
        mock_model = MagicMock()
        adapter._build_model_from_entry = MagicMock(return_value=mock_model)
        req = MagicMock()
        req.params = {"model_name": "model-a"}
        result = adapter._resolve_model_for_request(req)
        assert result is mock_model

    def test_model_name_not_in_shared_cache_raises(self):
        adapter = _make_stub_adapter(model_cache={})
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("gpt-4o", alias="fast", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"fast": entry}
        req = MagicMock()
        req.params = {"model_name": "gpt-4o"}
        with pytest.raises(ValueError, match="not found"):
            adapter._resolve_model_for_request(req)


# ---------------------------------------------------------------------------
# 6. _resolve_from_shared_model_cache logic
# ---------------------------------------------------------------------------


class TestResolveFromSharedModelCache:
    def test_returns_none_when_pool_missing(self):
        adapter = _make_stub_adapter()
        TenantAgentPool.reset_instance()
        result = adapter._resolve_from_shared_model_cache("model-a")
        assert result is None

    def test_returns_none_when_service_not_in_cache(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache.clear()
        result = adapter._resolve_from_shared_model_cache("model-a")
        assert result is None

    def test_returns_none_when_name_not_in_service_cache(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache["test-svc"] = {"other-model": _model_entry("other-model")}
        result = adapter._resolve_from_shared_model_cache("model-a")
        assert result is None

    def test_returns_none_when_entry_not_dict(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache["test-svc"] = {"model-a": "not-a-dict"}
        result = adapter._resolve_from_shared_model_cache("model-a")
        assert result is None

    def test_returns_none_when_model_name_empty(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("model-a")
        entry["model_client_config"]["model_name"] = ""
        pool._service_model_cache["test-svc"] = {"model-a": entry}
        result = adapter._resolve_from_shared_model_cache("model-a")
        assert result is None

    def test_builds_and_caches_model(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("model-a", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"model-a": entry}
        mock_model = MagicMock()
        adapter._build_model_from_entry = MagicMock(return_value=mock_model)
        result = adapter._resolve_from_shared_model_cache("model-a")
        assert result is mock_model
        assert adapter._model_cache["model-a"] is mock_model

    def test_builds_and_caches_model_by_alias(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("gpt-4o", alias="fast", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"fast": entry}
        mock_model = MagicMock()
        adapter._build_model_from_entry = MagicMock(return_value=mock_model)
        result = adapter._resolve_from_shared_model_cache("fast")
        assert result is mock_model
        assert adapter._model_cache["fast"] is mock_model

    def test_returns_none_on_build_exception(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("model-a", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"model-a": entry}
        adapter._build_model_from_entry = MagicMock(side_effect=RuntimeError("build failed"))
        result = adapter._resolve_from_shared_model_cache("model-a")
        assert result is None
        assert "model-a" not in adapter._model_cache


# ---------------------------------------------------------------------------
# 7. _merge_service_model_cache logic
# ---------------------------------------------------------------------------


class TestMergeServiceModelCache:
    def test_noop_when_pool_missing(self):
        adapter = _make_stub_adapter()
        TenantAgentPool.reset_instance()
        adapter._merge_service_model_cache()
        assert len(adapter._model_cache) == 0

    def test_noop_when_service_not_in_cache(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache.clear()
        adapter._merge_service_model_cache()
        assert len(adapter._model_cache) == 0

    def test_noop_when_service_cache_empty(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache["test-svc"] = {}
        adapter._merge_service_model_cache()
        assert len(adapter._model_cache) == 0

    def test_merges_new_entries_into_cache(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("model-a", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"model-a": entry}
        mock_model = MagicMock()
        adapter._build_model_from_entry = MagicMock(return_value=mock_model)
        adapter._merge_service_model_cache()
        assert adapter._model_cache["model-a"] is mock_model

    def test_merges_alias_entry_into_cache(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("gpt-4o", alias="fast", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"fast": entry}
        mock_model = MagicMock()
        adapter._build_model_from_entry = MagicMock(return_value=mock_model)
        adapter._merge_service_model_cache()
        assert "fast" in adapter._model_cache
        assert adapter._model_cache["fast"] is mock_model
        assert "gpt-4o" not in adapter._model_cache

    def test_skips_existing_keys(self):
        existing = MagicMock()
        adapter = _make_stub_adapter(model_cache={"model-a": existing})
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("model-a", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"model-a": entry}
        adapter._merge_service_model_cache()
        assert adapter._model_cache["model-a"] is existing

    def test_skips_non_dict_entries(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        pool._service_model_cache["test-svc"] = {"bad": "not-a-dict"}
        adapter._merge_service_model_cache()
        assert "bad" not in adapter._model_cache

    def test_skips_empty_model_name(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("model-a")
        entry["model_client_config"]["model_name"] = ""
        pool._service_model_cache["test-svc"] = {"model-a": entry}
        adapter._merge_service_model_cache()
        assert "model-a" not in adapter._model_cache

    def test_continues_on_build_exception(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry_a = _model_entry("model-a", api_base="https://api.test.com")
        entry_b = _model_entry("model-b", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {
            "model-a": entry_a,
            "model-b": entry_b,
        }

        def _build_side_effect(mcc, mco):
            if mcc.get("model_name") == "model-a":
                raise RuntimeError("build failed")
            return MagicMock()

        adapter._build_model_from_entry = MagicMock(side_effect=_build_side_effect)
        adapter._merge_service_model_cache()
        assert "model-a" not in adapter._model_cache
        assert "model-b" in adapter._model_cache

    def test_single_build_per_entry_with_alias(self):
        adapter = _make_stub_adapter()
        pool = TenantAgentPool.get_instance()
        entry = _model_entry("gpt-4o", alias="fast", api_base="https://api.test.com")
        pool._service_model_cache["test-svc"] = {"fast": entry}
        mock_model = MagicMock()
        adapter._build_model_from_entry = MagicMock(return_value=mock_model)
        adapter._merge_service_model_cache()
        assert adapter._build_model_from_entry.call_count == 1
        assert adapter._model_cache["fast"] is mock_model
