# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for sync_agents_configs catalog apply + env ns isolation."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.sync_agents_configs import (
    SYNC_ENV_SCHEMA,
    build_agent_spec,
    compute_content_hash,
    materialize_sync_env,
    validate_sync_payload,
)
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.server.runtime.tenant_catalog_registry import TenantCatalogRegistry
from jiuwenswarm.common.local_env_config import (
    BUSINESS_MIRROR_KEYS,
    EnvNsIdError,
    apply_env_overrides_to_active,
    bind_agent_env_ns,
    bind_task_env_overlay,
    export_agent_environ,
    get_active_env,
    get_local_config,
    get_staged_env,
    make_env_ns_key,
    mirror_bare_business_env_to_default_ns,
    normalize_env_ns_id,
    parse_env_ns_key,
    promote_staged_env,
    read_env_if_set,
    replace_active_env,
    reset_agent_env_ns,
    reset_local_env_state_for_tests,
    reset_task_env_overlay,
    set_os_environ,
    stage_env_overrides,
)
from jiuwenswarm.common.utils import resolve_tenant_sessions_dir


def _full_env(**overrides: str | None) -> dict[str, str | None]:
    """Minimal valid sync env with every SYNC_ENV_SCHEMA key present."""
    base: dict[str, str | None] = {key: "" for key in SYNC_ENV_SCHEMA}
    base.update(overrides)
    return base


def _sync_payload(
    *,
    revision: str = "rev-1",
    service_id: str = "default",
    agents: list[dict] | None = None,
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
    return {"revision": revision, "service_id": service_id, "agents": agents}


@pytest.fixture(autouse=True)
def _reset_state():
    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    for key in list(os.environ):
        if key.count("__") >= 2:
            del os.environ[key]
    TenantCatalogRegistry.reset_for_tests()
    TenantAgentPool.reset_instance()
    yield
    reset_local_env_state_for_tests()
    for key in list(os.environ):
        if key.count("__") >= 2:
            del os.environ[key]
    TenantCatalogRegistry.reset_for_tests()
    TenantAgentPool.reset_instance()
    os.environ.clear()
    os.environ.update(saved)
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def mock_warmup():
    """Avoid heavy create_instance during sync/warmup paths."""
    with patch.object(
        TenantAgentPool,
        "warmup_tenant",
        new=AsyncMock(return_value={"ok": True, "error": None}),
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# 1. ns / make_env_ns_key / parse roundtrip
# ---------------------------------------------------------------------------


class TestEnvNsKeyHelpers:
    @staticmethod
    def test_make_env_ns_key_roundtrip():
        full = make_env_ns_key("default", "office", "MODEL_NAME")
        assert full == "default__office__MODEL_NAME"
        parsed = parse_env_ns_key(full)
        assert parsed == ("default", "office", "MODEL_NAME")

    @staticmethod
    def test_id_with_double_underscore_raises():
        with pytest.raises(EnvNsIdError, match="__"):
            normalize_env_ns_id("bad__id")

    @staticmethod
    @pytest.mark.parametrize("value", ["../escape", "..\\escape", "/absolute", "\\absolute", "bad\x00id"])
    def test_id_with_path_syntax_raises(value: str):
        with pytest.raises(EnvNsIdError, match="path"):
            normalize_env_ns_id(value)

    @staticmethod
    @pytest.mark.parametrize("workspace_key", ["../escape", "..\\escape", "bad__id"])
    def test_tenant_sessions_dir_rejects_path_syntax(workspace_key: str):
        with pytest.raises(EnvNsIdError, match="path|__"):
            resolve_tenant_sessions_dir(workspace_key)

    @staticmethod
    def test_logical_key_with_double_underscore_raises():
        with pytest.raises(EnvNsIdError, match="logical env key"):
            make_env_ns_key("default", "office", "bad__key")

    @staticmethod
    def test_parse_rejects_invalid_keys():
        assert parse_env_ns_key("only__two") is None
        assert parse_env_ns_key("__office__MODEL") is None
        assert parse_env_ns_key("default__office__") is None


# ---------------------------------------------------------------------------
# 2. dual-track: Track A refused on Track B APIs
# ---------------------------------------------------------------------------


class TestDualTrackSpawnVsBusiness:
    @staticmethod
    def test_set_and_get_os_environ_refuses_home():
        os.environ["HOME"] = "/bare/home"
        set_os_environ("HOME", "/ns/home")
        assert get_local_config("HOME") is None
        assert os.environ["HOME"] == "/bare/home"
        assert "default__default__HOME" not in os.environ

    @staticmethod
    def test_bare_home_remains_in_os_environ():
        os.environ["HOME"] = "/spawn/home"
        assert os.environ.get("HOME") == "/spawn/home"


# ---------------------------------------------------------------------------
# 3. mirror_bare_business_env_to_default_ns
# ---------------------------------------------------------------------------


class TestMirrorBareBusinessEnv:
    @staticmethod
    def test_model_name_mirrored_home_not():
        os.environ["MODEL_NAME"] = "bare-model"
        os.environ["HOME"] = "/home/user"
        mirror_bare_business_env_to_default_ns()
        assert get_local_config("MODEL_NAME") == "bare-model"
        assert "default__default__HOME" not in os.environ
        assert os.environ["HOME"] == "/home/user"

    @staticmethod
    def test_force_false_is_idempotent():
        os.environ["MODEL_NAME"] = "m1"
        mirror_bare_business_env_to_default_ns()
        os.environ["MODEL_NAME"] = "m2"
        mirror_bare_business_env_to_default_ns(force=False)
        assert get_local_config("MODEL_NAME") == "m1"

    @staticmethod
    def test_get_default_models_env_fallback_needs_mirror():
        """Cold-start: get_local_config may fall through to bare os.environ; tip needs mirror."""
        from jiuwenswarm.common.local_env_config import read_env_if_set

        reset_local_env_state_for_tests()
        os.environ["API_BASE"] = "https://example.api"
        os.environ["API_KEY"] = "sk-bare"
        os.environ["MODEL_NAME"] = "bare-model"
        os.environ["MODEL_PROVIDER"] = "OpenAI"

        # Fallthrough keeps ${VAR} substitution working before ingest.
        assert get_local_config("MODEL_NAME") == "bare-model"
        # Tip-only reader still misses until mirror/ingest.
        assert read_env_if_set("MODEL_NAME") is None

        mirror_bare_business_env_to_default_ns()
        assert get_local_config("MODEL_NAME") == "bare-model"
        assert read_env_if_set("MODEL_NAME") == "bare-model"
        assert get_local_config("API_BASE") == "https://example.api"
        assert get_local_config("API_KEY") == "sk-bare"
        assert get_local_config("MODEL_PROVIDER") == "OpenAI"

    @staticmethod
    def test_config_set_and_business_read_keys_are_mirrored():
        """Scheme A: Web config.set + get_local_config business keys cold-start into tip."""
        # Keep in sync with app_web_handlers._CONFIG_SET_ENV_MAP values.
        config_set_env_keys = {
            "MODEL_PROVIDER",
            "MODEL_NAME",
            "API_BASE",
            "API_KEY",
            "VIDEO_API_BASE",
            "VIDEO_API_KEY",
            "VIDEO_MODEL_NAME",
            "VIDEO_PROVIDER",
            "AUDIO_API_BASE",
            "AUDIO_API_KEY",
            "AUDIO_MODEL_NAME",
            "AUDIO_PROVIDER",
            "VISION_API_BASE",
            "VISION_API_KEY",
            "VISION_MODEL_NAME",
            "VISION_PROVIDER",
            "IMAGE_GEN_API_BASE",
            "IMAGE_GEN_API_KEY",
            "IMAGE_GEN_MODEL_NAME",
            "IMAGE_GEN_PROVIDER",
            "EMAIL_ADDRESS",
            "EMAIL_TOKEN",
            "EMBED_API_KEY",
            "EMBED_API_BASE",
            "EMBED_MODEL",
            "JINA_API_KEY",
            "BOCHA_API_KEY",
            "SERPER_API_KEY",
            "PERPLEXITY_API_KEY",
            "GITHUB_TOKEN",
            "FREE_SEARCH_DDG_ENABLED",
            "FREE_SEARCH_BING_ENABLED",
            "FREE_SEARCH_PROXY_URL",
            "TOOL_CALLING_GUARD_ENABLED",
            "LLM_MODEL_NAME",
            "LLM_MODEL_TYPE",
            "LLM_BASE_URL",
            "LLM_API_KEY",
            "WEB_SEARCH_ENGINE_NAME",
            "WEB_SEARCH_API_KEY",
            "WEB_SEARCH_URL",
            "EXECUTION_METHOD",
        }
        missing_from_map = config_set_env_keys - BUSINESS_MIRROR_KEYS
        assert missing_from_map == set(), f"config.set keys missing from mirror: {missing_from_map}"

        extra_business_reads = {
            "ACR_ACCESS_KEY",
            "ACR_ACCESS_SECRET",
            "ACR_BASE_URL",
            "SKILLNET_DOWNLOAD_TIMEOUT",
            "SKILLNET_MAX_RETRIES",
            "OPENJIUWEN_MARKET_TIMEOUT",
            "OPENJIUWEN_MARKET_BASE_URL",
            "OPENJIUWEN_ALLOWED_DOWNLOAD_HOSTS",
            "IMPORT_LOCAL_REMOTE_TIMEOUT",
            "IMPORT_LOCAL_ALLOWED_DOWNLOAD_HOSTS",
            "TAVILY_API_KEY",
        }
        missing_reads = extra_business_reads - BUSINESS_MIRROR_KEYS
        assert missing_reads == set(), f"business read keys missing from mirror: {missing_reads}"

        os.environ["FREE_SEARCH_DDG_ENABLED"] = "1"
        os.environ["LLM_API_KEY"] = "sk-deep"
        os.environ["WEB_SEARCH_API_KEY"] = "sk-search"
        os.environ["ACR_ACCESS_KEY"] = "acr-key"
        os.environ["SKILLNET_DOWNLOAD_TIMEOUT"] = "90"
        mirror_bare_business_env_to_default_ns()

        assert get_local_config("FREE_SEARCH_DDG_ENABLED") == "1"
        assert get_local_config("LLM_API_KEY") == "sk-deep"
        assert get_local_config("WEB_SEARCH_API_KEY") == "sk-search"
        assert get_local_config("ACR_ACCESS_KEY") == "acr-key"
        assert get_local_config("SKILLNET_DOWNLOAD_TIMEOUT") == "90"


# ---------------------------------------------------------------------------
# 4. seal: bound {} blocks fallthrough
# ---------------------------------------------------------------------------


class TestTaskEnvSeal:
    @staticmethod
    def test_empty_overlay_seals_after_stage_and_promote():
        apply_env_overrides_to_active({"API_KEY": "active"}, service_id="default", agent_id="office")
        stage_env_overrides({"API_KEY": "staged"}, service_id="default", agent_id="office")
        promote_staged_env(service_id="default", agent_id="office")

        token = bind_task_env_overlay({})
        try:
            assert get_local_config("API_KEY") is None
            assert read_env_if_set("API_KEY") is None
        finally:
            reset_task_env_overlay(token)


# ---------------------------------------------------------------------------
# 5. formula B: staged wins over active when unbound
# ---------------------------------------------------------------------------


class TestFormulaB:
    @staticmethod
    def test_staged_wins_over_active_unbound():
        apply_env_overrides_to_active({"MODEL_NAME": "active"}, service_id="default", agent_id="default")
        stage_env_overrides({"MODEL_NAME": "staged"}, service_id="default", agent_id="default")
        assert get_local_config("MODEL_NAME") == "staged"


# ---------------------------------------------------------------------------
# 6. office / assistant isolation via ns bind
# ---------------------------------------------------------------------------


class TestOfficeAssistantIsolation:
    @staticmethod
    def test_office_value_not_visible_under_assistant_bind():
        apply_env_overrides_to_active(
            {"MODEL_NAME": "office-model"},
            service_id="default",
            agent_id="office",
        )
        apply_env_overrides_to_active(
            {"MODEL_NAME": "assistant-model"},
            service_id="default",
            agent_id="assistant",
        )

        token = bind_agent_env_ns("default", "assistant")
        try:
            assert get_local_config("MODEL_NAME") == "assistant-model"
            assert get_local_config("MODEL_NAME") != "office-model"
        finally:
            reset_agent_env_ns(token)


# ---------------------------------------------------------------------------
# 7. export_agent_environ
# ---------------------------------------------------------------------------


class TestExportAgentEnviron:
    @staticmethod
    def test_includes_deprefixed_b_and_present_spawn():
        apply_env_overrides_to_active(
            {"MODEL_NAME": "export-model", "API_KEY": "k"},
            service_id="default",
            agent_id="office",
        )
        os.environ["JIUWENSWARM_EDITION"] = "enterprise"
        os.environ["PATH"] = "/usr/bin"

        exported = export_agent_environ("default", "office")
        assert exported["MODEL_NAME"] == "export-model"
        assert exported["API_KEY"] == "k"
        assert exported["JIUWENSWARM_EDITION"] == "enterprise"
        assert exported["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# 8. enterprise rewrite vs env ns ids
# ---------------------------------------------------------------------------


class TestAgentRuntimeEnvNs:
    @staticmethod
    def test_manager_writes_default_office_not_office_default():
        os.environ["JIUWENSWARM_EDITION"] = "enterprise"
        manager = AgentManager(
            agent_id="office_default",
            service_id="default",
            env_agent_id="office",
            env_service_id="default",
            env_overrides={"MODEL_NAME": "runtime-model"},
        )
        assert manager.agent_id == "office_default"
        assert manager.env_agent_id == "office"
        assert get_active_env(service_id="default", agent_id="office")["MODEL_NAME"] == "runtime-model"
        assert "MODEL_NAME" not in get_active_env(service_id="office_default", agent_id="default")


# ---------------------------------------------------------------------------
# 9. sync_agents_configs
# ---------------------------------------------------------------------------


class TestSyncAgentsConfigsValidation:
    @staticmethod
    def test_validate_rejects_missing_env_key():
        with pytest.raises(ValueError, match="missing required keys"):
            validate_sync_payload(
                {
                    "revision": "r1",
                    "service_id": "default",
                    "agents": [
                        {
                            "agent_id": "office",
                            "config": {},
                            "env": {"API_KEY": "x"},
                            "runtime": {},
                        }
                    ],
                }
            )

    @staticmethod
    def test_materialize_omits_null_keeps_empty():
        env = materialize_sync_env({"API_KEY": "k", "MODEL_NAME": None, "API_BASE": ""})
        assert env == {"API_KEY": "k", "API_BASE": ""}


@pytest.mark.asyncio
async def test_sync_first_adds_agents_registry_contains(mock_warmup):
    pool = TenantAgentPool.get_instance()
    result = await pool.sync_agents_configs(_sync_payload(revision="rev-add"))

    assert result["revision"] == "rev-add"
    assert result["agents"][0]["action"] in {"added", "updated"}
    assert result["agents"][0]["ok"] is True

    registry = TenantCatalogRegistry.get_instance()
    assert registry.contains("default", "office")


@pytest.mark.asyncio
async def test_sync_same_revision_fast_path_unchanged(mock_warmup):
    pool = TenantAgentPool.get_instance()
    payload = _sync_payload(revision="same-rev")

    first = await pool.sync_agents_configs(payload)
    assert first["agents"][0]["action"] in {"added", "updated"}
    warmup_calls_after_first = mock_warmup.await_count

    second = await pool.sync_agents_configs(payload)
    assert second["agents"][0]["action"] == "unchanged"
    assert mock_warmup.await_count == warmup_calls_after_first


@pytest.mark.asyncio
async def test_sync_skills_revision_forces_live_agent_update(mock_warmup):
    pool = TenantAgentPool.get_instance()

    first = await pool.sync_agents_configs(_sync_payload(revision="r1", agents=[
        {
            "agent_id": "office",
            "config": {},
            "env": _full_env(ENABLED_SKILLS="same-skill"),
            "runtime": {"skills_revision": 0},
        }
    ]))
    assert first["agents"][0]["action"] in {"added", "updated"}

    unchanged = await pool.sync_agents_configs(_sync_payload(revision="r2", agents=[
        {
            "agent_id": "office",
            "config": {},
            "env": _full_env(ENABLED_SKILLS="same-skill"),
            "runtime": {"skills_revision": 0},
        }
    ]))
    assert unchanged["agents"][0]["action"] == "unchanged"

    changed = await pool.sync_agents_configs(_sync_payload(revision="r3", agents=[
        {
            "agent_id": "office",
            "config": {},
            "env": _full_env(ENABLED_SKILLS="same-skill"),
            "runtime": {"skills_revision": 1},
        }
    ]))
    assert changed["agents"][0]["action"] == "updated"


@pytest.mark.asyncio
async def test_sync_update_model_name_replaces_tip(mock_warmup):
    pool = TenantAgentPool.get_instance()
    await pool.sync_agents_configs(_sync_payload(revision="r1", agents=[
        {
            "agent_id": "office",
            "config": {},
            "env": _full_env(MODEL_NAME="old-model"),
            "runtime": {},
        }
    ]))

    result = await pool.sync_agents_configs(_sync_payload(revision="r2", agents=[
        {
            "agent_id": "office",
            "config": {},
            "env": _full_env(MODEL_NAME="new-model"),
            "runtime": {},
        }
    ]))

    assert result["agents"][0]["action"] == "updated"
    assert get_active_env(service_id="default", agent_id="office")["MODEL_NAME"] == "new-model"


@pytest.mark.asyncio
async def test_sync_null_env_key_removed_from_tip(mock_warmup):
    pool = TenantAgentPool.get_instance()
    await pool.sync_agents_configs(_sync_payload(revision="r1", agents=[
        {
            "agent_id": "office",
            "config": {},
            "env": _full_env(MODEL_NAME="keep-me", API_KEY="secret"),
            "runtime": {},
        }
    ]))
    assert "API_KEY" in get_active_env(service_id="default", agent_id="office")

    await pool.sync_agents_configs(_sync_payload(revision="r2", agents=[
        {
            "agent_id": "office",
            "config": {},
            "env": _full_env(MODEL_NAME="keep-me", API_KEY=None),
            "runtime": {},
        }
    ]))

    active = get_active_env(service_id="default", agent_id="office")
    assert active.get("MODEL_NAME") == "keep-me"
    assert "API_KEY" not in active


@pytest.mark.asyncio
async def test_sync_empty_agents_removes_all(mock_warmup):
    pool = TenantAgentPool.get_instance()
    await pool.sync_agents_configs(_sync_payload(revision="r1"))

    registry = TenantCatalogRegistry.get_instance()
    assert registry.contains("default", "office")

    result = await pool.sync_agents_configs(
        {"revision": "r2", "service_id": "default", "agents": []}
    )
    assert result["agents"][0]["action"] == "removed"
    assert registry.contains("default", "office") is False
    assert get_active_env(service_id="default", agent_id="office") == {}


@pytest.mark.asyncio
async def test_sync_preempt_clears_staged_rebuilds_pending(mock_warmup):
    pool = TenantAgentPool.get_instance()
    await pool.sync_agents_configs(_sync_payload(revision="r1"))

    agent_manager = await pool._ensure_agent_manager("office", "default", "default")
    mock_adapter = MagicMock()
    mock_adapter.is_working.return_value = True
    mock_adapter._pending_reload = ("old-config", {"MODEL_NAME": "old"})

    def _queue_pending_reload(config_base, env_overrides):
        mock_adapter._pending_reload = (config_base, env_overrides)

    mock_adapter.queue_pending_reload.side_effect = _queue_pending_reload

    mock_claw = MagicMock()
    mock_claw.is_working.return_value = True
    mock_claw._adapter = mock_adapter
    # apply_sync_config uses public ensure_adapter (not private _ensure_adapter).
    mock_claw.ensure_adapter = MagicMock(return_value=mock_adapter)
    agent_manager.agents["officeclaw"] = {"agent": mock_claw}

    stage_env_overrides(
        {"MODEL_NAME": "staged-before-sync"},
        service_id="default",
        agent_id="office",
    )
    assert get_staged_env(service_id="default", agent_id="office")["MODEL_NAME"] == "staged-before-sync"

    new_env = _full_env(MODEL_NAME="sync-preempt-model")
    await pool.sync_agents_configs(_sync_payload(revision="r2", agents=[
        {
            "agent_id": "office",
            "config": {"react": {"agent_name": "office-v2"}},
            "env": new_env,
            "runtime": {},
        }
    ]))

    assert get_staged_env(service_id="default", agent_id="office") == {}
    pending = mock_adapter._pending_reload
    assert pending is not None
    assert pending[0]["react"]["agent_name"] == "office-v2"
    assert pending[1]["MODEL_NAME"] == "sync-preempt-model"


@pytest.mark.asyncio
@pytest.mark.skip(reason="target sync path skips warmup_tenant; failure is not gated on warmup")
async def test_sync_half_failure_does_not_elevate_revision(mock_warmup):
    pool = TenantAgentPool.get_instance()
    mock_warmup.return_value = {"ok": False, "error": "warmup boom"}

    result = await pool.sync_agents_configs(_sync_payload(revision="fail-rev"))
    assert result["agents"][0]["ok"] is False
    assert pool._last_sync_revision.get("default") is None
    # Catalog must not commit on soft failure; otherwise identical retries short-circuit.
    assert TenantCatalogRegistry.get_instance().contains("default", "office") is False

    mock_warmup.return_value = {"ok": True, "error": None}
    second = await pool.sync_agents_configs(_sync_payload(revision="fail-rev", agents=[
        {
            "agent_id": "office",
            "config": {},
            "env": _full_env(MODEL_NAME="retry-after-fail"),
            "runtime": {},
        }
    ]))
    # First attempt never committed, so retry is still an add (or update if seeded).
    assert second["agents"][0]["action"] in {"added", "updated"}
    assert second["agents"][0]["ok"] is True
    assert pool._last_sync_revision.get("default") == "fail-rev"


@pytest.mark.asyncio
async def test_sync_replace_active_env_failure_allows_identical_retry(mock_warmup):
    """upsert must not precede env apply: failed env leave catalog unset so retries re-apply."""
    pool = TenantAgentPool.get_instance()
    payload = _sync_payload(
        revision="env-fail-rev",
        agents=[
            {
                "agent_id": "office",
                "config": {},
                "env": _full_env(API_KEY="rotated-key", MODEL_NAME="m"),
                "runtime": {},
            }
        ],
    )
    registry = TenantCatalogRegistry.get_instance()

    with patch(
        "jiuwenswarm.server.runtime.tenant_agent_pool.replace_active_env",
        side_effect=RuntimeError("env write failed"),
    ):
        first = await pool.sync_agents_configs(payload)

    assert first["agents"][0]["ok"] is False
    assert "env write failed" in (first["agents"][0].get("error") or "")
    assert registry.contains("default", "office") is False
    assert get_active_env(service_id="default", agent_id="office").get("API_KEY") != "rotated-key"

    second = await pool.sync_agents_configs(payload)
    assert second["agents"][0]["ok"] is True
    assert second["agents"][0]["action"] == "added"
    assert registry.contains("default", "office") is True
    assert get_active_env(service_id="default", agent_id="office")["API_KEY"] == "rotated-key"


@pytest.mark.asyncio
@pytest.mark.skip(reason="target sync path skips warmup_tenant; failure is not gated on warmup")
async def test_sync_warmup_failure_identical_payload_retries_apply(mock_warmup):
    pool = TenantAgentPool.get_instance()
    payload = _sync_payload(
        revision="warmup-fail-rev",
        agents=[
            {
                "agent_id": "office",
                "config": {},
                "env": _full_env(API_KEY="k1", MODEL_NAME="m1"),
                "runtime": {},
            }
        ],
    )
    registry = TenantCatalogRegistry.get_instance()

    mock_warmup.return_value = {"ok": False, "error": "warmup boom"}
    first = await pool.sync_agents_configs(payload)
    assert first["agents"][0]["ok"] is False
    assert registry.contains("default", "office") is False
    # Env may already be tip-updated; catalog hash must still allow retry.
    assert get_active_env(service_id="default", agent_id="office").get("API_KEY") == "k1"

    mock_warmup.return_value = {"ok": True, "error": None}
    second = await pool.sync_agents_configs(payload)
    assert second["agents"][0]["ok"] is True
    assert second["agents"][0]["action"] == "added"
    assert registry.contains("default", "office") is True
    assert registry.get("default", "office").content_hash is not None
    assert mock_warmup.await_count >= 1


@pytest.mark.asyncio
async def test_sync_reload_failed_does_not_commit_catalog(mock_warmup):
    pool = TenantAgentPool.get_instance()
    payload = _sync_payload(
        revision="reload-fail-rev",
        agents=[
            {
                "agent_id": "office",
                "config": {"react": {"agent_name": "office"}},
                "env": _full_env(API_KEY="secret", MODEL_NAME="m"),
                "runtime": {},
            }
        ],
    )
    registry = TenantCatalogRegistry.get_instance()

    reload_result = MagicMock()
    reload_result.applied = 0
    reload_result.deferred = 0
    reload_result.failed = [{"session": "x", "error": "boom"}]
    mock_manager = MagicMock()
    mock_manager.apply_sync_config = AsyncMock(return_value=reload_result)

    with patch.object(
        TenantAgentPool,
        "_ensure_agent_manager",
        new=AsyncMock(return_value=mock_manager),
    ):
        first = await pool.sync_agents_configs(payload)

    assert first["agents"][0]["ok"] is False
    assert registry.contains("default", "office") is False

    reload_ok = MagicMock()
    reload_ok.applied = 1
    reload_ok.deferred = 0
    reload_ok.failed = []
    mock_manager.apply_sync_config = AsyncMock(return_value=reload_ok)
    with patch.object(
        TenantAgentPool,
        "_ensure_agent_manager",
        new=AsyncMock(return_value=mock_manager),
    ):
        second = await pool.sync_agents_configs(payload)

    assert second["agents"][0]["ok"] is True
    assert second["agents"][0]["action"] == "added"
    assert registry.contains("default", "office") is True


@pytest.mark.asyncio
async def test_replace_active_clears_staged_formula_b():
    stage_env_overrides(
        {"MODEL_NAME": "staged-old"},
        service_id="default",
        agent_id="office",
    )
    replace_active_env(
        {"MODEL_NAME": "sync-new"},
        service_id="default",
        agent_id="office",
        clear_staged=True,
    )
    assert get_staged_env(service_id="default", agent_id="office") == {}
    assert get_active_env(service_id="default", agent_id="office")["MODEL_NAME"] == "sync-new"


@pytest.mark.asyncio
async def test_sync_agents_configs_registers_and_isolates_tip(mock_warmup):
    pool = TenantAgentPool.get_instance()
    payload = _sync_payload(
        revision="rev-iso",
        agents=[
            {
                "agent_id": "office",
                "config": {"react": {"agent_name": "office"}},
                "env": _full_env(MODEL_NAME="office-model", API_KEY="ok"),
                "runtime": {},
            },
            {
                "agent_id": "assistant",
                "config": {"react": {"agent_name": "assistant"}},
                "env": _full_env(MODEL_NAME="assistant-model", API_KEY="ak"),
                "runtime": {},
            },
        ],
    )

    result = await pool.sync_agents_configs(payload)
    assert {a["agent_id"] for a in result["agents"]} == {"assistant", "office"}
    assert all(a["ok"] for a in result["agents"])

    registry = TenantCatalogRegistry.get_instance()
    assert registry.contains("default", "office")
    assert registry.contains("default", "assistant")

    assert get_active_env(service_id="default", agent_id="office")["MODEL_NAME"] == "office-model"
    assert (
        get_active_env(service_id="default", agent_id="assistant")["MODEL_NAME"]
        == "assistant-model"
    )


def test_build_agent_spec_hash_stable():
    a = build_agent_spec(
        service_id="default",
        agent_id="office",
        config={},
        env=_full_env(MODEL_NAME="x"),
        runtime={},
        revision="r1",
    )
    b = build_agent_spec(
        service_id="default",
        agent_id="office",
        config={},
        env=_full_env(MODEL_NAME="x"),
        runtime={},
        revision="r1",
    )
    assert a.content_hash == b.content_hash


def test_compute_content_hash_does_not_scan_shared_skill_directories(tmp_path, monkeypatch):
    """Catalog hash must stay metadata-only; Relay already sends effective skill/config state."""
    shared_dir = tmp_path / "skills"
    shared_dir.mkdir()
    env = _full_env(JIUWENSWARM_SHARED_SKILLS_DIRS=str(shared_dir))

    smart = shared_dir / "smart-charts"
    smart.mkdir()
    (smart / "SKILL.md").write_text("# smart-charts")

    def fail_if_scanned(*_args, **_kwargs):
        raise AssertionError("content hash must not scan shared skill directories")

    monkeypatch.setattr(os, "scandir", fail_if_scanned)
    first = compute_content_hash(config={}, env=env, runtime={})

    another = shared_dir / "another-skill"
    another.mkdir()
    (another / "SKILL.md").write_text("# another-skill")
    second = compute_content_hash(config={}, env=env, runtime={})
    assert first == second

    assert first != compute_content_hash(config={"prompt": "changed"}, env=env, runtime={})
    assert first != compute_content_hash(
        config={}, env={**env, "ENABLED_SKILLS": "another-skill"}, runtime={}
    )
    assert first != compute_content_hash(
        config={}, env=env, runtime={"skills": ["another-skill"]}
    )


def test_compute_content_hash_no_shared_dirs_is_stable():
    """env 无 JIUWENSWARM_SHARED_SKILLS_DIRS 时不扫盘、hash 稳定（不产生磁盘 I/O）。"""
    env = _full_env(MODEL_NAME="x")
    a = compute_content_hash(config={}, env=env, runtime={})
    b = compute_content_hash(config={}, env=env, runtime={})
    assert a == b


# ---------------------------------------------------------------------------
# 10. officeclaw guard
# ---------------------------------------------------------------------------


class TestOfficeclawGuard:
    @staticmethod
    def test_require_officeclaw_missing_ids_use_legacy_default_tenant():
        from jiuwenswarm.common.schema.agent import AgentRequest

        req = AgentRequest(
            request_id="r1",
            channel_id="officeclaw",
            agent_id=None,
            service_id=None,
            params={},
        )
        assert TenantAgentPool.require_officeclaw_agent(req) is None
        assert TenantAgentPool.extract_ids(req) == ("default", "default", "default")

    @staticmethod
    def test_require_officeclaw_tenant_not_registered():
        from jiuwenswarm.common.schema.agent import AgentRequest

        req = AgentRequest(
            request_id="r1",
            channel_id="officeclaw",
            agent_id="office",
            service_id="default",
            params={},
        )
        guard = TenantAgentPool.require_officeclaw_agent(req)
        assert guard is not None
        assert guard.payload["code"] == "tenant_not_registered"

    @staticmethod
    def test_require_officeclaw_ok_after_registry_upsert():
        from jiuwenswarm.common.schema.agent import AgentRequest

        registry = TenantCatalogRegistry.get_instance()
        registry.upsert(
            build_agent_spec(
                service_id="default",
                agent_id="office",
                config={},
                env=_full_env(),
                runtime={},
                revision="r1",
            )
        )

        req = AgentRequest(
            request_id="r1",
            channel_id="officeclaw",
            agent_id="office",
            service_id="default",
            params={},
        )
        assert TenantAgentPool.require_officeclaw_agent(req) is None

