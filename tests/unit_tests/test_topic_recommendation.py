# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Topic recommendation: switches, consent, config, store, channel, and startup."""

from __future__ import annotations

# --- test_recommendation_switches.py ---

from jiuwenswarm.agents.harness.common.recommendation import switches

def test_null_yaml_follows_code_defaults():
    config = {"recommendation": {"topic_enabled": None, "prompt_enabled": None}}
    assert switches.is_topic_recommendation_enabled(config) is switches.DEFAULT_TOPIC_RECOMMENDATION_ENABLED
    assert switches.is_prompt_suggestion_enabled(config) is switches.DEFAULT_PROMPT_SUGGESTION_ENABLED
    assert switches.DEFAULT_TOPIC_RECOMMENDATION_ENABLED is False
    assert switches.DEFAULT_PROMPT_SUGGESTION_ENABLED is True

def test_explicit_true_and_false_override_defaults():
    on = {"recommendation": {"topic_enabled": True, "prompt_enabled": False}}
    assert switches.is_topic_recommendation_enabled(on) is True
    assert switches.is_prompt_suggestion_enabled(on) is False
    off = {"recommendation": {"topic_enabled": "false", "prompt_enabled": "true"}}
    assert switches.is_topic_recommendation_enabled(off) is False
    assert switches.is_prompt_suggestion_enabled(off) is True

def test_legacy_enabled_without_consent_stays_off():
    config = {"recommendation": {"enabled": True}}
    assert switches.is_topic_recommendation_enabled(config) is False
    # Prompt suggestion follows its own code default; legacy ``enabled`` is not consent.
    assert switches.is_prompt_suggestion_enabled(config) is switches.DEFAULT_PROMPT_SUGGESTION_ENABLED

def test_explicit_null_split_keys_ignore_legacy_enabled():
    config = {
        "recommendation": {
            "enabled": True,
            "consent_version": "recommendation-consent-v2",
            "topic_enabled": None,
            "prompt_enabled": None,
        }
    }
    assert switches.is_topic_recommendation_enabled(config) is False
    assert switches.is_prompt_suggestion_enabled(config) is switches.DEFAULT_PROMPT_SUGGESTION_ENABLED

def test_save_topic_switch_records_history_boundary(user_config):
    switches.save_recommendation_switches(topic_enabled=True, now=12.5)
    section = user_config.recommendation
    assert section["topic_enabled"] is True
    assert "prompt_enabled" not in section
    # The boundary is state, not configuration: it is frozen beside the recommendation data.
    assert "history_after" not in section
    assert "updated_at" not in section
    assert controls.load_recommendation_controls()["history_after"] == 12.5
    switches.save_recommendation_switches(prompt_enabled=True, now=13.0)
    assert user_config.recommendation["prompt_enabled"] is True
    assert controls.load_recommendation_controls()["history_after"] == 12.5


def test_common_save_does_not_import_agents_when_hook_missing(user_config, tmp_path, monkeypatch):
    """common switches stay layer-clean; freeze is hook-only, heal via repair on startup."""
    from jiuwenswarm.common import recommendation_switches as common_switches

    monkeypatch.setattr(controls, "get_agent_workspace_dir", lambda: tmp_path)
    hooks = list(common_switches._TOPIC_ENABLED_HOOKS)
    common_switches._TOPIC_ENABLED_HOOKS.clear()
    try:
        common_switches.save_recommendation_switches(topic_enabled=True, now=42.0)
        assert controls._consent_state()["history_after"] is None
        assert controls.repair_missing_consent_boundary(now=42.0) is True
        assert controls.load_recommendation_controls()["history_after"] == 42.0
    finally:
        common_switches._TOPIC_ENABLED_HOOKS[:] = hooks


def test_repair_missing_consent_boundary_when_topics_already_enabled(
    user_config, tmp_path, monkeypatch,
):
    """Enabled YAML without a store boundary is healed without requiring toggle off/on."""
    from jiuwenswarm.common import config as config_module

    monkeypatch.setattr(controls, "get_agent_workspace_dir", lambda: tmp_path)

    def enable_without_freeze(cfg):
        section = cfg.setdefault("recommendation", {})
        section["topic_enabled"] = True
        return cfg

    config_module.update_config(enable_without_freeze)
    assert controls._consent_state()["history_after"] is None
    assert controls.repair_missing_consent_boundary(now=99.0) is True
    assert controls.load_recommendation_controls()["history_after"] == 99.0
    assert controls.repair_missing_consent_boundary(now=100.0) is False


def test_prompt_suggestion_turns_are_revisit_eligible_like_hand_typed():
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        V6SourceMessage,
        _summary_origins,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
        is_user_revisit_eligible,
    )

    assert is_user_revisit_eligible("manual_free_text", "none")
    assert is_user_revisit_eligible("prompt_suggestion", "prompt_suggestion")
    assert not is_user_revisit_eligible("starter_click", "starter_click")
    mixed = (
        V6SourceMessage(
            "e1", "s", 0, "手打问题", 1.0,
            "manual_free_text", "none", "cluster", "natural_question", None,
        ),
        V6SourceMessage(
            "e2", "s", 1, "提示建议问题", 2.0,
            "prompt_suggestion", "prompt_suggestion", "cluster", "natural_question", None,
        ),
    )
    assert all(message.natural for message in mixed)
    assert _summary_origins(mixed) == ("manual_free_text", "none")

def test_recommendation_switches_reload_ui_only():
    from jiuwenswarm.common.config_panel.config_set_handlers import ConfigChangeSet, needs_agent_reload

    for key in ("topic_recommendation_enabled", "prompt_suggestion_enabled"):
        change = ConfigChangeSet({}, [key])
        assert change.reload_scopes == {"web_ui"}
        assert needs_agent_reload(change.reload_scopes) is False
    assert needs_agent_reload({"model"}) is True
    assert needs_agent_reload({"agent_runtime"}) is True
    assert needs_agent_reload(set()) is True

# --- test_topic_controls.py ---

import asyncio
import json
import threading

import pytest
import yaml

from jiuwenswarm.agents.harness.common.recommendation import topic_controls as controls
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import TopicStarterStore
from jiuwenswarm.agents.harness.common.recommendation.topic_service import TopicService
from jiuwenswarm.server.runtime.session.session_history import project_completed_session_prefix, project_completed_session_records

@pytest.fixture
def settings(monkeypatch):
    value = {}
    monkeypatch.setattr(controls, "get_config", lambda: value)
    monkeypatch.setattr(controls, "update_config", lambda mutate: mutate(value))
    return value

def _records():
    return [{"id": "old:user", "request_id": "old", "role": "user", "content": "private old history", "timestamp": 1.0},
            {"id": "new:user", "request_id": "new", "role": "user", "content": "new independent request", "timestamp": 3.0},
            {"id": "new:request_completed", "request_id": "new", "role": "assistant", "timestamp": 4.0,
             "event_type": "chat.request_completed", "feedback_only": True, "status": "success"}]


class _UserConfig:
    """The temp config.yaml the recommendation migrations read and write, with writes counted."""

    def __init__(self, path, writes):
        self.path = path
        self.writes = writes

    @property
    def recommendation(self) -> dict:
        document = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        section = document.get("recommendation")
        return section if isinstance(section, dict) else {}

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    """Point both config readers at one temp file, counting every write.

    ``switches`` resolves ``jiuwenswarm.common.config`` lazily and ``topic_controls`` binds it at
    import, so the module attributes and the ``topic_controls`` names both have to move.
    """
    from jiuwenswarm.common import config as config_module

    path = tmp_path / "config.yaml"
    path.write_text("recommendation: {}\n", encoding="utf-8")
    writes: list[int] = []
    real_update = config_module.update_config

    def counting_update(mutator, **kwargs):
        writes.append(1)
        return real_update(mutator, **kwargs)

    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    monkeypatch.setattr(config_module, "update_config", counting_update)
    monkeypatch.setattr(controls, "get_config", config_module.get_config)
    monkeypatch.setattr(controls, "update_config", counting_update)
    return _UserConfig(path, writes)


@pytest.fixture
def shipped_template(tmp_path) -> str:
    from importlib import resources

    return resources.files("jiuwenswarm.resources").joinpath("config.yaml").read_text(encoding="utf-8")

def test_new_and_unconsented_upgrade_start_disabled(settings):
    assert controls.load_recommendation_controls()["enabled"] is False
    settings["recommendation"] = {"enabled": True}
    assert controls.load_recommendation_controls()["enabled"] is False
    assert "private old history" not in json.dumps(controls.permitted_recommendation_history(_records()))

def test_accepted_consent_stays_in_force_across_reenable(settings):
    settings["recommendation"] = {"topic_enabled": True}
    controls.save_recommendation_controls(enabled=True, now=2.0)
    control = controls.load_recommendation_controls()
    assert (control["enabled"], control["history_after"]) == (True, 2.0)
    assert controls.permitted_recommendation_history(_records(), control)[1] == _records()[1]
    controls.save_recommendation_controls(enabled=False, now=5.0)
    controls.save_recommendation_controls(enabled=True, now=6.0)
    # Re-enabling reuses the boundary the first opt-in froze rather than moving it forward.
    assert controls.load_recommendation_controls()["history_after"] == 2.0
    assert "consent_version" not in settings["recommendation"]

def test_consent_default_boundary_and_explicit_history_reuse(settings):
    controls.save_recommendation_controls(enabled=True, now=2.0)
    rows = controls.permitted_recommendation_history(_records())
    assert rows[0]["role"] == "recommendation_history_excluded"
    assert rows[1] == _records()[1]
    assert len(rows) == len(_records())
    controls.save_recommendation_controls(enabled=False, now=5.0)
    controls.save_recommendation_controls(enabled=True, now=6.0)
    assert controls.load_recommendation_controls()["history_after"] == 2.0
    controls.save_recommendation_controls(enabled=True, include_history=True, now=7.0)
    assert controls.permitted_recommendation_history(_records()) == _records()

# --- the consent state, and the one-off moves out of config.yaml ---

def test_the_retired_enabled_key_migrates_onto_both_switches(user_config):
    user_config.write(
        "recommendation:\n"
        "  enabled: true\n"
        "  consent_version: recommendation-consent-v2\n"
        "  history_after: 4.5\n"
        "  updated_at: 9.0\n"
        "  model: mdl_1\n"
    )
    assert controls.migrate_legacy_recommendation_switches() is True
    section = user_config.recommendation
    assert (section["topic_enabled"], section["prompt_enabled"]) == (True, True)
    assert section["model"] == "mdl_1"
    assert "enabled" not in section and "consent_version" not in section


def test_the_legacy_key_without_accepted_consent_migrates_to_the_default(user_config):
    user_config.write("recommendation:\n  enabled: true\n")
    assert controls.migrate_legacy_recommendation_switches() is True
    assert user_config.recommendation["topic_enabled"] is False
    assert user_config.recommendation["prompt_enabled"] is False


def test_a_switch_the_user_chose_survives_the_migration(user_config):
    """The template merge injects ``null``; a null must read as unset, not as the user's choice."""
    user_config.write(
        "recommendation:\n"
        "  enabled: true\n"
        "  consent_version: recommendation-consent-v2\n"
        "  topic_enabled: null\n"
        "  prompt_enabled: false\n"
    )
    controls.migrate_legacy_recommendation_switches()
    section = user_config.recommendation
    assert section["topic_enabled"] is True    # repaired from the retired key
    assert section["prompt_enabled"] is False  # an explicit choice wins


def test_the_template_merge_cannot_retire_the_legacy_key(user_config, shipped_template):
    """The merge skips AgentOS, so retiring the key is the standalone migration's job.

    Pins the hazard with it: the merge adds the template's ``null`` switches while keeping the
    retired key, so a user who had it on reads as off.
    """
    from jiuwenswarm.common.config import migrate_config_from_template

    template = user_config.path.parent / "template.yaml"
    template.write_text(shipped_template, encoding="utf-8")
    user_config.write(
        "recommendation:\n  enabled: true\n  consent_version: recommendation-consent-v2\n"
    )
    migrate_config_from_template(template, user_config.path)
    assert "enabled" in user_config.recommendation
    assert controls.load_recommendation_controls()["enabled"] is False
    assert controls.migrate_legacy_recommendation_switches() is True
    assert controls.load_recommendation_controls()["enabled"] is True


def test_a_migrated_config_is_not_reinjected_by_the_next_template_merge(user_config, shipped_template):
    """Dropping the keys from the template is what keeps the merge from adding them back."""
    from jiuwenswarm.common.config import migrate_config_from_template

    template = user_config.path.parent / "template.yaml"
    template.write_text(shipped_template, encoding="utf-8")
    document = yaml.safe_load(shipped_template)
    document["recommendation"] = {"enabled": True, "consent_version": "recommendation-consent-v2",
                                  "history_after": 4.5, "updated_at": 9.0, "deleted_at": None,
                                  "model": "follow_main"}
    user_config.path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    controls.migrate_legacy_recommendation_switches()
    controls.migrate_recommendation_consent_state()
    assert set(user_config.recommendation) == {"topic_enabled", "prompt_enabled", "model"}
    migrate_config_from_template(template, user_config.path)
    assert set(user_config.recommendation) == {"topic_enabled", "prompt_enabled", "model"}


def test_the_state_migration_moves_the_values_and_is_idempotent(user_config):
    user_config.write("recommendation:\n  history_after: 4.5\n  deleted_at: 9.0\n")
    assert controls.migrate_recommendation_consent_state() is True
    assert user_config.recommendation == {}
    control = controls.load_recommendation_controls()
    assert (control["history_after"], control["deleted_at"]) == (4.5, 9.0)
    writes = len(user_config.writes)
    assert controls.migrate_recommendation_consent_state() is False
    assert controls.migrate_legacy_recommendation_switches() is False
    assert len(user_config.writes) == writes


def test_the_deletion_epoch_survives_the_data_deletion(tmp_path):
    import sqlite3

    store = TopicStarterStore(tmp_path / "recommendation.db")
    controls.save_recommendation_controls(enabled=False, deleted=True, now=7.0, store=store)
    tables = {row[0] for row in sqlite3.connect(store.path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "recommendation_consent_state" in tables
    store.delete_recommendation_data()
    control = controls.load_recommendation_controls(store)
    assert (control["history_after"], control["deleted_at"]) == (7.0, 7.0)


def test_an_unreadable_store_hides_history_instead_of_raising(user_config, tmp_path):
    """A damaged store degrades to "nothing recorded"; it never takes the caller down."""

    class _BrokenStore:
        path = tmp_path / "recommendation.db"

    _BrokenStore.path.mkdir()  # a directory where the database belongs
    control = controls.load_recommendation_controls(_BrokenStore())
    assert control["history_after"] is None and control["deleted_at"] is None
    assert controls.permitted_at(9.0, control) is False


def _topic_service(tmp_path, *, enabled: bool = False, now: float = 2.0) -> TopicService:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig

    async def prepare(_config):
        return "no_eligible_history"

    return TopicService(
        TopicStarterStore(tmp_path / "recommendation.db"), generate=None,
        search_provider_id="test", prepare_state=prepare,
        config_loader=lambda: ArkBackgroundConfig(api_key="test", model="test",
                                                  context_window_tokens=131072),
        enabled=enabled, now=lambda: now,
    )


@pytest.mark.asyncio
async def test_preparation_reads_the_store_off_the_worker_loop(tmp_path, monkeypatch):
    """The prepare gate decodes the profile snapshot and the newest manifests.

    Both are SQLite reads that can wait behind a writer, so they belong on the
    executor rather than on the Runtime Worker's event loop.
    """

    from jiuwenswarm.agents.harness.common.recommendation import topic_service as service_module
    from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
        load_latest_history_state,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    loop_thread = threading.get_ident()
    reads: list[int] = []

    class _RecordingStore(TopicStarterStore):
        def query_events(self, *args, **kwargs):
            reads.append(threading.get_ident())
            return super().query_events(*args, **kwargs)

    def recording_loader(store):
        reads.append(threading.get_ident())
        return load_latest_history_state(store)

    async def prepare_state(_config):
        return "no_eligible_history"

    service = TopicService(
        _RecordingStore(tmp_path / "recommendation-loop-thread.db"),
        generate=None,
        search_provider_id="test",
        prepare_state=prepare_state,
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=True,
        now=lambda: 2.0,
    )
    monkeypatch.setattr(service_module, "load_latest_history_state", recording_loader)
    try:
        await service.schedule_successor(
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            visit_id="visit-loop-thread",
        )
    finally:
        await service.shutdown()

    assert reads
    assert loop_thread not in reads


@pytest.mark.asyncio
async def test_the_config_echo_puts_the_switch_in_force_without_writing_config(user_config, tmp_path):
    service = _topic_service(tmp_path)
    try:
        await service.set_enabled(True, persist=False)
        assert user_config.writes == []
        assert service.configuration_status()["enabled"] is True
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_reload_config_swaps_model_without_reseeding(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig

    configs = [
        ArkBackgroundConfig(api_key="test", model="old-model", context_window_tokens=131072),
        ArkBackgroundConfig(api_key="test", model="new-model", context_window_tokens=131072),
    ]
    seeds: list[str] = []

    async def prepare(_config):
        return "no_eligible_history"

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation.db"),
        generate=None,
        search_provider_id="test",
        prepare_state=prepare,
        config_loader=lambda: configs[0],
        enabled=True,
        now=lambda: 2.0,
    )
    original_schedule = service.schedule_seed

    def capture_seed(visit_id: str):
        seeds.append(visit_id)
        return original_schedule(visit_id)

    service.schedule_seed = capture_seed  # type: ignore[method-assign]
    try:
        configs[0] = configs[1]
        status = await service.reload_config()
        assert status["model"] == "new-model"
        assert service.ark_config is not None
        assert service.ark_config.model == "new-model"
        assert "recommendation-model-changed" not in seeds
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_missing_model_reports_missing_configuration_without_reading_history(tmp_path):
    """Without a model no pool can be written; history must not be read to learn that."""

    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
    )

    reads: list[tuple[str, str]] = []
    prepared: list[tuple[tuple, dict]] = []

    def input_revision(scope, mode):
        reads.append((scope, mode))
        raise AssertionError("history must not be read without a model")

    async def prepare_state(_config):
        raise AssertionError("preparation must not run without a model")

    async def prepare_short_intent(_scope):
        raise AssertionError("short intent must not run without a model")

    async def generate(*_args, **_kwargs):
        raise AssertionError("generation must not run without a model")

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation-missing-model.db"),
        generate=generate,
        search_provider_id="test",
        prepare_state=prepare_state,
        prepare_short_intent=prepare_short_intent,
        input_revision_loader=input_revision,
        config_loader=lambda: None,
        enabled=True,
        now=lambda: 2.0,
    )

    def capture(*args, **kwargs):
        prepared.append((args, kwargs))

    service.on_prepared = capture
    try:
        outcome = await service.schedule_successor(
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            visit_id="visit-missing-model",
        )
        await asyncio.sleep(0)
        assert outcome == "missing_configuration"
        assert reads == []
        assert prepared == [
            (
                (GLOBAL_NEW_CHAT_SCOPE, "missing_configuration", False),
                {"work_mode": "work"},
            ),
        ]
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_deleting_the_data_writes_no_configuration(user_config, tmp_path):
    """The switch is the entry point's job; delete only records the epoch and wipes the data."""
    service = _topic_service(tmp_path)
    try:
        await service.delete_data()
        assert user_config.writes == []
        assert controls.load_recommendation_controls()["deleted_at"] == 2.0
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_packaged_template_requires_fresh_consent(tmp_path, monkeypatch):
    from importlib import resources
    from jiuwenswarm.common import config
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig

    path = tmp_path / "config.yaml"
    path.write_text(
        resources.files("jiuwenswarm.resources").joinpath("config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    control = controls.load_recommendation_controls()
    assert control["enabled"] is False
    assert control["history_after"] is None
    calls = []

    async def prepare(_config):
        calls.append("prepare")
        return "no_eligible_history"

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation.db"), generate=None,
        search_provider_id="test", prepare_state=prepare,
        config_loader=lambda: ArkBackgroundConfig(api_key="test", model="test", context_window_tokens=131072),
        enabled=control["enabled"], now=lambda: 2.0,
    )
    assert await service.schedule_seed("fresh-install") == "disabled"
    assert calls == []
    await service.set_enabled(True)
    assert controls.load_recommendation_controls()["history_after"] == 2.0
    assert controls.permitted_recommendation_history(_records())[0]["role"] == "recommendation_history_excluded"
    await service.set_enabled(True, include_history=True)
    assert controls.load_recommendation_controls()["history_after"] == 0.0
    await service.shutdown()

def test_profile_and_click_sources_hide_rewritten_or_unconsented_history(tmp_path, settings, monkeypatch):
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history
    controls.save_recommendation_controls(enabled=True, include_history=True, now=2.0)
    records = _records()[1:]
    state = history.empty_history_state()
    state["history_ledger"] = history.rescan_history(None, {"session": records}, {}).ledger
    state["memory"]["units"] = [{"core": {"preference_summary": "保留独立事项"}, "companion": {"unit_id": "unit-1", "canonical_source_locators": [["session", 0]]}}]
    store = TopicStarterStore(tmp_path / "profile.db")
    history.save_history_state(store, state, now=4.0)
    monkeypatch.setattr(history, "load_recommendation_source_records", lambda sid: records)
    profile = controls.inspect_recommendation_profile(store, now=5.0)
    assert profile["units"][0]["sources"][0]["content"] == "new independent request"
    records[0] = {**records[0], "content": "rewritten source"}
    assert controls.inspect_recommendation_profile(store, now=5.0)["units"] == []
    controls.save_recommendation_controls(enabled=False, deleted=True, now=5.0)
    assert controls.recommendation_sources(store, [["session", 0]]) == []

def test_profile_reads_each_source_session_once(tmp_path, settings, monkeypatch):
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history
    controls.save_recommendation_controls(enabled=True, include_history=True, now=2.0)
    records = _records()[1:]
    state = history.empty_history_state()
    state["history_ledger"] = history.rescan_history(None, {"session": records}, {}).ledger
    state["memory"]["units"] = [
        {"core": {"preference_summary": "第一个单元"},
         "companion": {"unit_id": "unit-1", "canonical_source_locators": [["session", 0]]}},
        {"core": {"preference_summary": "第二个单元"},
         "companion": {"unit_id": "unit-2", "canonical_source_locators": [["session", 0]]}},
    ]
    store = TopicStarterStore(tmp_path / "profile.db")
    history.save_history_state(store, state, now=4.0)
    reads: list[str] = []

    def fake_load(session_id: str):
        reads.append(session_id)
        return records

    monkeypatch.setattr(history, "load_recommendation_source_records", fake_load)
    profile = controls.inspect_recommendation_profile(store, now=5.0)

    # Both units cite the same source session; one profile read must project it once.
    assert reads == ["session"]
    assert len(profile["units"]) == 2

# --- test_topic_configuration.py ---

import json

import pytest

from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig, background_configuration_status,
)

def test_missing_configuration_is_secret_free_and_separate_from_chat():
    values = {"ARK_API_KEY": "private-test-key", "ARK_MODEL": "deepseek-v4-flash-260425"}
    state = background_configuration_status(ArkBackgroundConfig.from_environ(values), values)
    assert state["missing_fields"] == ["ARK_CONTEXT_WINDOW_TOKENS"]
    assert state["status"] == "missing_configuration"
    assert state["provider"] == "recommendation"
    assert "private-test-key" not in json.dumps(state)

_CHAT_MODELS = """
models:
  defaults:
    - model_id: mdl_chat
      model_client_config:
        model_name: chat-model
        api_base: https://chat.example/api/v3
        api_key: ${ARK_API_KEY}
        client_provider: OpenAI
      model_config_obj: {}
    - model_id: mdl_long
      model_client_config:
        model_name: long-model
        api_base: https://long.example/v1
        api_key: long-key
        client_provider: OpenAI
      model_config_obj:
        context_window: 65536
    - model_id: mdl_messages
      model_client_config:
        model_name: messages-model
        api_base: https://messages.example
        api_key: messages-key
        client_provider: Anthropic
      model_config_obj: {}
    - model_id: mdl_account
      model_client_config:
        model_name: account-model
        api_base: https://account.example
        api_key: ""
        client_provider: OpenAIAccount
      model_config_obj: {}
    - model_id: mdl_template
      model_client_config:
        model_name: ""
        client_provider: OpenAI
      model_config_obj: {}
"""

@pytest.fixture
def chat_config(monkeypatch, tmp_path):
    """config.yaml as the chat path reads it, with only ARK_API_KEY for its placeholder in the environment."""
    from jiuwenswarm.common import config as config_module

    path = tmp_path / "config.yaml"
    path.write_text(_CHAT_MODELS, encoding="utf-8")
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    for name in ("ARK_MODEL", "ARK_CONTEXT_WINDOW_TOKENS", "ARK_BASE_URL", "ARK_EXPECTED_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_API_KEY", "placeholder-test-key")
    return path

def _resolved():
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        recommendation_model_config,
    )
    return recommendation_model_config()

@pytest.mark.parametrize("selection", [None, "", "follow_main", "removed-model"])
def test_unconfigured_topics_do_not_inherit_a_chat_model(chat_config, selection):
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        recommendation_model_options,
    )

    controls.update_config(lambda config: {**config, "recommendation": {"model": selection}})
    assert _resolved() is None
    options = recommendation_model_options(None)
    assert options["selected_model"] == ""
    assert options["configured_model"] == ""
    assert options["model_options"] == []

def test_a_chosen_model_is_kept_in_config_and_decides_the_model(chat_config):
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        load_recommendation_controls, save_recommendation_model,
    )

    assert save_recommendation_model("mdl_long") is True
    assert "model: mdl_long" in chat_config.read_text(encoding="utf-8")
    config = _resolved()
    assert (config.model, config.base_url, config.context_window_tokens) == (
        "long-model", "https://long.example/v1", 65536)
    # Choosing a model is not consent.
    assert load_recommendation_controls()["enabled"] is False
    assert "consent_version" not in chat_config.read_text(encoding="utf-8")

    assert save_recommendation_model("follow_main") is False
    assert _resolved().model == "long-model"

def test_clearing_topic_model_turns_off_topics_and_preserves_chat_models(chat_config):
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        load_recommendation_controls, recommendation_model_options,
        save_recommendation_model,
    )

    assert save_recommendation_model("mdl_long")
    switches.save_recommendation_switches(topic_enabled=True, now=1234.0)
    assert load_recommendation_controls()["enabled"] is True
    assert recommendation_model_options(None)["configured_model"] == "mdl_long"
    assert save_recommendation_model("")
    assert load_recommendation_controls()["enabled"] is False
    options = recommendation_model_options(None)
    assert options["configured_model"] == ""
    assert options["model_options"] == []


def test_deleting_dedicated_topic_model_persists_disabled_state_on_disk(chat_config):
    import yaml

    assert controls.save_recommendation_model_configuration({
        "provider": "OpenAI", "model": "dedicated-topic-model",
        "api_base": "https://topics.example/v1", "api_key": "test-topic-key",
        "context_window_tokens": "128k",
    })
    switches.save_recommendation_switches(topic_enabled=True, now=1234.0)
    assert controls.save_recommendation_model("")
    saved = yaml.safe_load(chat_config.read_text(encoding="utf-8"))
    assert saved["recommendation"]["topic_enabled"] is False
    assert "model_config" not in saved["recommendation"]
    assert controls.recommendation_model_configuration() == {}
    assert controls.load_recommendation_controls()["enabled"] is False
    assert saved["models"], "deleting a dedicated topic model must preserve chat models"


def test_clearing_topic_model_does_not_validate_the_model_catalog(settings, monkeypatch):
    settings["recommendation"] = {"model": "removed-model", "topic_enabled": True}

    def unavailable_catalog(config):
        raise AssertionError("Clearing a binding must not read the model catalog")

    monkeypatch.setattr(controls, "get_default_models", unavailable_catalog)
    assert controls.save_recommendation_model("")
    assert settings["recommendation"] == {"model": "", "topic_enabled": False}


@pytest.mark.asyncio
async def test_delete_topic_model_rpc_disables_runtime_and_notifies_clients(settings, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from jiuwenswarm.server.runtime import recommendation_rpc

    settings["recommendation"] = {"model": "removed-model", "topic_enabled": True}
    service = SimpleNamespace(reload_config=AsyncMock(), set_enabled=AsyncMock())
    host = SimpleNamespace(topic_service=service, ensure_services=Mock())
    monkeypatch.setattr(recommendation_rpc, "_status_payload", AsyncMock(return_value={
        "configured_model": "", "enabled": False,
    }))
    result = await recommendation_rpc.dispatch_recommendation(
        host, action="select_model", params={"model": ""}, session_id="test", request_id="delete-model",
    )
    assert result.ok is True
    assert result.payload["configured_model"] == ""
    assert settings["recommendation"]["topic_enabled"] is False
    service.set_enabled.assert_awaited_once_with(False, persist=False)
    assert ("topic_starters.control_changed", {"enabled": False}) in result.events


def test_dedicated_topic_configuration_does_not_add_chat_models(settings):
    settings["models"] = [{"model_id": "original-chat-model"}]
    values = {"model": "topic-only", "provider": "OpenAI", "api_base": "https://topics.example/v1",
              "api_key": "test-key", "vendor_key": "", "plan": "",
              "context_window_tokens": "256k"}
    assert controls.save_recommendation_model_configuration(values)
    assert settings["models"] == [{"model_id": "original-chat-model"}]
    config = controls.recommendation_model_config(environ={})
    assert config.model == "topic-only"
    assert config.context_window_tokens == 256 * 1024
    public = controls.recommendation_model_configuration()
    assert public["has_api_key"] is True
    assert public["context_window_tokens"] == 256 * 1024
    assert "api_key" not in public
    assert controls.recommendation_model_configuration(include_api_key=True)["api_key"] == "test-key"
    assert controls.save_recommendation_model_configuration({
        **values, "model": "edited", "api_key": "", "context_window_tokens": "512K",
    })
    assert settings["recommendation"]["model_config"]["api_key"] == "test-key"
    assert settings["recommendation"]["model_config"]["context_window_tokens"] == 512 * 1024
    assert controls.recommendation_model_config(environ={}).context_window_tokens == 512 * 1024
    assert not controls.save_recommendation_model_configuration({**values, "api_key": "", "api_base": "https://other.example"})
    assert not controls.save_recommendation_model_configuration({
        **{key: value for key, value in values.items() if key != "context_window_tokens"},
    })
    assert controls.save_recommendation_model("")
    assert controls.recommendation_model_configuration() == {}
    assert settings["recommendation"]["topic_enabled"] is False


@pytest.mark.asyncio
async def test_dedicated_model_rpc_saves_without_disabling_an_enabled_switch(settings, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from jiuwenswarm.server.runtime import recommendation_rpc

    settings["recommendation"] = {"topic_enabled": True}
    service = SimpleNamespace(reload_config=AsyncMock(), set_enabled=AsyncMock())
    host = SimpleNamespace(topic_service=service, ensure_services=Mock())
    monkeypatch.setattr(recommendation_rpc, "_status_payload", AsyncMock(return_value={"enabled": True}))
    result = await recommendation_rpc.dispatch_recommendation(
        host, action="select_model", params={"model": "", "configuration": {
            "provider": "OpenAI", "model": "topics", "api_base": "https://topics.example/v1",
            "api_key": "test-key", "context_window_tokens": "128k",
        }}, session_id="test", request_id="configure-model",
    )
    assert result.ok is True
    assert settings["recommendation"]["model_config"]["model"] == "topics"
    assert settings["recommendation"]["model_config"]["context_window_tokens"] == 128 * 1024
    assert settings["recommendation"]["topic_enabled"] is True
    service.reload_config.assert_awaited_once()
    service.set_enabled.assert_not_awaited()
    assert result.events == []


def test_legacy_dedicated_topic_model_gets_default_context_window(settings):
    settings["recommendation"] = {"model_config": {
        "provider": "OpenAI", "model": "topics", "api_base": "https://topics.example/v1",
        "api_key": "saved-test-key",
    }}
    config = controls.recommendation_model_config(environ={})
    assert config is not None
    assert config.context_window_tokens == controls.RECOMMENDATION_CONTEXT_WINDOW_TOKENS
    public = controls.recommendation_model_configuration()
    assert public["context_window_tokens"] == controls.RECOMMENDATION_CONTEXT_WINDOW_TOKENS


@pytest.mark.asyncio
async def test_status_returns_saved_key_only_when_explicitly_requested(settings, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from jiuwenswarm.server.runtime import recommendation_rpc

    settings["recommendation"] = {"model_config": {
        "provider": "OpenAI", "model": "topics", "api_base": "https://topics.example/v1",
        "api_key": "saved-test-key",
    }}
    monkeypatch.setattr(controls, "recommendation_model_options", Mock(return_value={}))
    service = SimpleNamespace(configuration_status=Mock(return_value={"configured": True}))
    host = SimpleNamespace(topic_service=service, ensure_services=Mock())
    for params, includes_key in [({}, False), ({"include_api_key": "true"}, False), ({"include_api_key": True}, True)]:
        result = await recommendation_rpc.dispatch_recommendation(
            host, action="status", params=params, session_id="test", request_id="edit-model",
        )
        assert result.ok
        assert ("api_key" in result.payload["model_config"]) is includes_key
        assert result.payload["model_config"]["context_window_tokens"] == controls.RECOMMENDATION_CONTEXT_WINDOW_TOKENS
        if includes_key:
            assert result.payload["model_config"]["api_key"] == "saved-test-key"


def test_legacy_chat_binding_is_not_a_dedicated_configuration(settings):
    settings["recommendation"] = {"model": "GLM-5.1"}
    assert controls.recommendation_model_configuration() == {}


def test_a_complete_ark_environment_still_overrides_the_choice(chat_config, monkeypatch):
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        save_recommendation_model,
    )

    assert save_recommendation_model("mdl_long")
    monkeypatch.setenv("ARK_MODEL", "experiment-model")
    monkeypatch.setenv("ARK_CONTEXT_WINDOW_TOKENS", "32768")
    # An endpoint is part of a complete override. Without it the chosen chat model stays.
    assert _resolved().model == "long-model"
    monkeypatch.setenv("ARK_BASE_URL", "https://experiment.example/v1")
    config = _resolved()
    assert (config.model, config.base_url, config.context_window_tokens, config.api_key) == (
        "experiment-model", "https://experiment.example/v1", 32768, "placeholder-test-key")

def test_turning_recommendations_on_keeps_the_chosen_model(chat_config):
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        save_recommendation_controls, save_recommendation_model,
    )

    assert save_recommendation_model("mdl_long")
    save_recommendation_controls(enabled=True, now=5.0)
    assert _resolved().model == "long-model"

# --- test_topic_store.py ---

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openjiuwen.core.single_agent import AgentCard

from jiuwenswarm.agents.harness.common.recommendation import topic_starter
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
    TopicStarterService,
    compute_interest_drift,
)

def test_interest_drift_uses_paper_weights_and_threshold() -> None:
    no_evidence = compute_interest_drift([], [])
    assert no_evidence.score == 0.0
    assert no_evidence.should_update is False

    unchanged = compute_interest_drift(
        ["ai", "research", "ai", "research"],
        ["ai", "research", "ai", "research"],
    )
    assert unchanged.score == 0.0
    assert unchanged.should_update is False

    changed = compute_interest_drift(
        ["ai", "ai", "ai", "ai"],
        ["travel", "food", "music", "sports", "finance", "history", "film", "health"],
    )
    assert changed.entropy_delta == 3.0
    assert changed.jaccard_distance == 1.0
    assert changed.js_divergence == 1.0
    assert changed.score == 1.8
    assert changed.should_update is True

def test_topic_starter_store_keeps_only_two_tables_and_deduplicates_exposure(
    tmp_path: Path,
) -> None:
    store = TopicStarterStore(tmp_path / "recommendation.db")
    snapshot = store.save_snapshot(
        {
            "interests": [
                {
                    "id": "ai-research",
                    "label": "AI research",
                    "keywords": ["AI", "research"],
                    "evidence_refs": ["session-a:user-1"],
                }
            ],
            "evidence_labels": [
                {"evidence_id": "session-a:user-1", "interest_id": "ai-research"}
            ],
        },
        evidence_until=10.0,
    )

    assert snapshot["version"] == 1
    assert store.load_latest_snapshot() == snapshot

    event = {
        "session_id": "new-session",
        "slate_id": "slate-1",
        "event_type": "dynamic_exposure",
        "topic_id": None,
        "position": None,
        "profile_version": 1,
        "policy_id": "topic-starter-mvp-v1",
        "selection_mode": "personalized_dynamic",
        "propensity": None,
        "propensity_reason": "not_estimated",
        "payload": {"topics": ["topic-1"]},
    }
    assert store.record_event(event) is True
    assert store.record_event(event) is False
    assert len(store.list_events()) == 1
    with sqlite3.connect(tmp_path / "recommendation.db") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {"profile_snapshot", "recommendation_event"}

def _component_result(*, selection_mode: str = "personalized_dynamic") -> dict:
    retrieved_at = "2026-08-20T12:00:01+00:00"
    return {
        "available": True,
        "slate_id": "slate-smoke",
        "session_id": "v3-component-acceptance",
        "profile_version": 2,
        "policy_id": "topic-starter-mvp-v3",
        "selection_mode": selection_mode,
        "retrieval_health": "eligible",
        "topics": [
            {
                "id": f"topic-{index}",
                "title": f"Topic {index}",
                "prompt": f"Discuss topic {index}",
                "evidence": [
                    {
                        "url": f"https://example.com/{index}",
                        "title": f"Source {index}",
                        "retrieved_at": retrieved_at,
                    }
                ],
            }
            for index in range(4)
        ],
    }

_RUNTIME_RECENT_PLAN = [
    {
        "intent": "recent_interest",
        "query": "AKOrN H0 H1",
        "anchor_terms": ["AKOrN", "H0", "H1"],
        "grounding_required": True,
    }
]

class FakeAgent:
    def __init__(self, output: dict):
        self.output = output
        self.calls: list[dict] = []

    async def invoke(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"output": json.dumps(self.output, ensure_ascii=False)}

def _grounded_topics(
    intents: list[str] | None = None,
    *,
    anchor_text: str = "",
) -> list[dict]:
    intents = intents or ["generic_discovery"] * 4
    return [
        {
            "id": f"topic-{index}",
            "title": f"Topic {index} {anchor_text}".strip(),
            "prompt": f"Tell me about topic {index} {anchor_text}".strip(),
            "intent": intent,
            "evidence": [
                {
                    "url": f"https://example.com/{index}",
                    "title": f"Source {index}",
                    "retrieved_at": "2026-08-20T12:00:00Z",
                }
            ],
        }
        for index, intent in enumerate(intents, start=1)
    ]

class FakeFetchPage:
    def __init__(self, page_text: str):
        self.page_text = page_text
        self.calls: list[str] = []

    async def __call__(self, url: str) -> str:
        self.calls.append(url)
        return self.page_text

_FETCHED_PAGE_TEXT = (
    "URL: https://example.com\nStatus: 200\n"
    "Title: AI AKOrN RLVR robotics current report\n"
    "Content:\nAI AKOrN RLVR robotics evidence"
)

def _profile_interest(
    interest_id: str,
    label: str,
    keywords: list[str],
    evidence_refs: list[str] | None = None,
) -> dict:
    return {
        "id": interest_id,
        "label": label,
        "keywords": keywords,
        "confidence": 0.8,
        "evidence_refs": evidence_refs or [],
        "first_observed_at": "2026-08-01T00:00:00Z",
        "last_observed_at": "2026-08-20T00:00:00Z",
    }

# --- test_topic_lifecycle.py ---

from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    empty_history_state,
    load_latest_history_state,
    save_history_state,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
    NewPageOutcomeQueue,
    NewPageOutcomeSignal,
    TopicStarterStoreOutcomeSink,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
    GLOBAL_NEW_CHAT_SCOPE,
    POLICY_ID,
    InterestFeedback,
)

LIFECYCLE_NOW = 1_800_000_000.0
LIFECYCLE_REQUEST_IDS = (
    ("icebreaker_rid", "request-rid-1"),
    ("pa_bridge_fixed_retriever", "request-pa-1"),
)
LIFECYCLE_PROVIDER_RESPONSE_IDS = (
    "provider-response-session-1",
    "provider-response-rid-1",
)
LIFECYCLE_SEARCH_RECEIPT = {
    "receipt_version": "topic-starter-v6-ark-responses-web-search-v1",
    "provider_id": "ark-responses-web-search",
    "provider_response_id": "search-response-1",
    "response_object": "response",
    "response_status": "completed",
    "actual_model": "doubao-seed-test",
    "web_search_call_id": "web-search-call-1",
    "action": {"type": "search", "query": "search query 1"},
    "usage": {"tool_usage": {"web_search": 1}},
    "result_count": 1,
    "requested_query": "search query 1",
    "engine": "ark-responses-web-search",
}

def _ledger_event(
    *,
    event_type: str,
    slate_id: str,
    topic_id: str,
    payload: dict[str, Any],
    session_id: str = "topic-starter-background",
    created_at: float = LIFECYCLE_NOW,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "slate_id": slate_id,
        "event_type": event_type,
        "topic_id": topic_id,
        "policy_id": POLICY_ID,
        "selection_mode": "personalized_dynamic",
        "propensity_reason": "not_estimated",
        "payload": payload,
        "created_at": created_at,
    }

def _install_exact_exposure(
    store: TopicStarterStore,
    *,
    source_memory_unit_ids: tuple[str, ...] = ("memory-causal-evaluation",),
    interest: dict[str, Any] | None = None,
    scope: tuple[str, str | None] = (GLOBAL_NEW_CHAT_SCOPE, None),
    source_locators: list[list[Any]] | None = None,
) -> None:
    context_scope, project_id = scope
    receipts = {
        component_id: {
            "binding": {"provider_version": "doubao-seed-test"},
            "request_id": request_id,
        }
        for component_id, request_id in LIFECYCLE_REQUEST_IDS
    }
    assert store.record_event(
        _ledger_event(
            event_type="starter_slate_manifest",
            slate_id="manifest-1",
            topic_id="manifest-1",
            payload={
                "manifest_id": "manifest-1",
                "generation_batch_id": "batch-1",
                "card_revision_ids": ["revision-1"],
                "context_scope": context_scope,
                "project_id": project_id,
                **({"rid_result": {"trigger_interests": [interest]}} if interest else {}),
            },
        )
    )
    assert store.record_event(
        _ledger_event(
            event_type="starter_card",
            slate_id="manifest-1",
            topic_id="revision-1",
            payload={
                "manifest_id": "manifest-1",
                "generation_batch_id": "batch-1",
                "card_revision_id": "revision-1",
                "context_scope": context_scope,
                "project_id": project_id,
                "profile_semantic_version": 3,
                "intent": "causal recommendation evaluation",
                "title": "How should causal recommendation be evaluated?",
                "prompt": "Compare causal recommendation evaluation protocols",
                "source_memory_unit_ids": list(source_memory_unit_ids),
                "source_locators": source_locators or [],
                **({"interest_summary_id": interest["interest_summary_id"]} if interest else {}),
                "source_starter_id": "starter-1",
                "generation_input_snapshot_id": "input-snapshot-1",
                "component_receipts": receipts,
                "component_invocation_receipts": [
                    {
                        "schema_version": (
                            "topic-starter-v6-component-invocation-v2"
                        ),
                        "component_id": component_id,
                        "request_id": request_id,
                        "input_snapshot_id": "input-snapshot-1",
                        "binding_scope": (
                            "all_cards"
                            if component_id == "icebreaker_rid"
                            else "starter"
                        ),
                        "candidate_ids": [],
                        "starter_ids": (
                            []
                            if component_id == "icebreaker_rid"
                            else ["starter-1"]
                        ),
                    }
                    for component_id, request_id in LIFECYCLE_REQUEST_IDS
                ],
                "search_provider_id": "ark-responses-web-search",
                "search_receipt": LIFECYCLE_SEARCH_RECEIPT,
                "provider_invocation_receipts": [
                    {"provider_response_id": response_id}
                    for response_id in LIFECYCLE_PROVIDER_RESPONSE_IDS
                ],
            },
        )
    )
    _install_exposure(store, "exposure-1", scope=scope)

def _install_exposure(
    store: TopicStarterStore,
    exposure_id: str,
    *,
    scope: tuple[str, str | None] = (GLOBAL_NEW_CHAT_SCOPE, None),
) -> None:
    context_scope, project_id = scope
    assert store.record_event(
        _ledger_event(
            event_type="dynamic_exposure",
            slate_id=exposure_id,
            topic_id="manifest-1",
            session_id="new-page-session",
            created_at=LIFECYCLE_NOW + 1,
            payload={
                "manifest_id": "manifest-1",
                "generation_batch_id": "batch-1",
                "exposure_id": exposure_id,
                "card_revision_ids": ["revision-1"],
                "context_scope": context_scope,
                "project_id": project_id,
                "provider_response_ids_by_card_revision": {
                    "revision-1": list(LIFECYCLE_PROVIDER_RESPONSE_IDS)
                },
                "search_receipts_by_card_revision": {
                    "revision-1": LIFECYCLE_SEARCH_RECEIPT
                },
            },
        )
    )

def _signal(
    *,
    outcome: str = "accepted",
    component_request_ids: tuple[tuple[str, str], ...] = LIFECYCLE_REQUEST_IDS,
    provider_response_ids: tuple[str, ...] = LIFECYCLE_PROVIDER_RESPONSE_IDS,
    scope: tuple[str, str | None] = (GLOBAL_NEW_CHAT_SCOPE, None),
    exposure_id: str = "exposure-1",
) -> NewPageOutcomeSignal:
    return NewPageOutcomeSignal(
        outcome=outcome,
        manifest_id="manifest-1",
        generation_batch_id="batch-1",
        exposure_id=exposure_id,
        card_revision_id="revision-1",
        context_scope=scope[0],
        project_id=scope[1],
        component_request_ids=component_request_ids,
        provider_response_ids=provider_response_ids,
        search_receipt=LIFECYCLE_SEARCH_RECEIPT,
        occurred_at=LIFECYCLE_NOW + 2,
    )

def test_queue_drains_pre_attach_outcome_to_real_store_sink(tmp_path: Any) -> None:
    store = TopicStarterStore(tmp_path / "queue.sqlite3")
    _install_exact_exposure(store)
    queue = NewPageOutcomeQueue()

    assert queue.publish(_signal()) == "queued"
    assert queue.pending_count == 1
    assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == [True]
    assert queue.pending_count == 0

    events = store.query_events(
        event_type="topic_click",
        slate_id="exposure-1",
        topic_id="revision-1",
        policy_id=POLICY_ID,
    )
    assert len(events) == 1
    assert events[0]["payload"]["manifest_id"] == "manifest-1"
    assert events[0]["payload"]["generation_batch_id"] == "batch-1"
    assert events[0]["payload"]["context_scope"] == GLOBAL_NEW_CHAT_SCOPE
    assert events[0]["payload"]["provider_response_ids"] == list(
        LIFECYCLE_PROVIDER_RESPONSE_IDS
    )
    assert events[0]["payload"]["search_receipt"] == LIFECYCLE_SEARCH_RECEIPT

def test_outcome_signal_rejects_not_interested() -> None:
    try:
        _signal(outcome="not_interested")
    except ValueError as exc:
        assert "accepted" in str(exc)
    else:
        raise AssertionError("not_interested outcome must be rejected")

# --- test_topic_web_channel.py ---

import asyncio
import json
from typing import Any

import pytest

@pytest.fixture
def enabled_recommendation_consent(monkeypatch):
    from jiuwenswarm.agents.harness.common.recommendation import topic_controls
    monkeypatch.setattr(topic_controls, "load_recommendation_controls", lambda: {"enabled": True})

from agent_gateway.channel_manager.base import RobotMessageRouter  # pylint: disable=wrong-import-order
from agent_gateway.channel_manager.web.app_web_handlers import (  # pylint: disable=wrong-import-order
    WebHandlersBindParams,
    _register_web_handlers,
)
from agent_gateway.channel_manager.web.web_connect import (  # pylint: disable=wrong-import-order
    WebChannel,
    WebChannelConfig,
)

from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
    NewPageOutcomeQueue,
    TopicStarterStoreOutcomeSink,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
    GLOBAL_NEW_CHAT_SCOPE,
    POLICY_ID,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_service import (
    TopicService,
)

CHANNEL_NOW = 1_700_000_000.0
CHANNEL_REQUEST_IDS = (
    ("icebreaker_rid", "request-rid-1"),
    ("pa_bridge_fixed_retriever", "request-pa-1"),
)
CHANNEL_PROVIDER_RESPONSE_IDS = ("provider-response-1",)
CHANNEL_SEARCH_RECEIPT = {
    "receipt_version": "topic-starter-v6-ark-responses-web-search-v1",
    "provider_id": "ark-responses-web-search",
    "provider_response_id": "search-response-1",
    "response_object": "response",
    "response_status": "completed",
    "actual_model": "doubao-seed-test",
    "web_search_call_id": "web-search-call-1",
    "action": {"type": "search", "query": "search query 1"},
    "usage": {"tool_usage": {"web_search": 1}},
    "result_count": 1,
    "requested_query": "search query 1",
    "engine": "ark-responses-web-search",
}

class _OpenWebSocket:
    def __init__(self, path: str) -> None:
        self.path = path
        self.remote_address = ("127.0.0.1", 12345)
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def request(self, request_id: str, method: str, params: dict[str, Any]) -> None:
        self._queue.put_nowait(
            json.dumps(
                {
                    "type": "req",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        )

    def release(self) -> None:
        self._queue.put_nowait(None)

    async def send(self, data: str | bytes) -> None:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self.closed = True

    def __aiter__(self) -> _OpenWebSocket:
        return self

    async def __anext__(self) -> str:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

async def _wait_response(ws: _OpenWebSocket, request_id: str) -> dict[str, Any]:
    for _ in range(500):
        for frame in ws.sent:
            if frame.get("type") == "res" and frame.get("id") == request_id:
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError(f"no response for {request_id}")

def _channel_event(
    *,
    event_type: str,
    slate_id: str,
    topic_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "session_id": "background",
        "slate_id": slate_id,
        "event_type": event_type,
        "topic_id": topic_id,
        "policy_id": POLICY_ID,
        "selection_mode": "personalized_dynamic",
        "propensity_reason": "not_estimated",
        "payload": payload,
        "created_at": CHANNEL_NOW,
    }

class _TopicServiceStub:
    record_interaction = TopicService.record_interaction
    # A recommendation model is in force, so a read does not reload the configuration.
    ark_config = object()
    def schedule_news_check(self, *_args, **_kwargs):
        return None

    def __init__(self, store: TopicStarterStore) -> None:
        self.store = store
        self._news_refresh_task = None
        self.serve_calls: list[tuple[str, str, str | None]] = []
        self.exposure_calls: list[dict[str, Any]] = []
        self.successor_calls: list[tuple[str, str | None, str]] = []
        self._enabled = True

    def configuration_status(self) -> dict[str, Any]:
        return {"enabled": self._enabled}

    async def set_enabled(self, enabled: bool, *, persist: bool = False) -> None:
        self._enabled = enabled

    def serve(
        self,
        session_id: str,
        *,
        context_scope: str,
        project_id: str | None,
        work_mode: str = "work",
    ) -> dict[str, Any]:
        del work_mode
        self.serve_calls.append((session_id, context_scope, project_id))
        return {
            "available": True,
            "session_id": session_id,
            "slate_id": "exposure-1",
            "manifest_id": "manifest-1",
            "generation_batch_id": "batch-1",
            "profile_version": 3,
            "policy_id": POLICY_ID,
            "context_scope": context_scope,
            "project_id": project_id,
            "short_intent_version": 1,
            "pool_card_count": 7,
            "exposure_slice_id": "manifest-1:window:0",
            "selection_mode": "personalized_dynamic",
            "topics": [
                {
                    "id": "revision-1",
                    "card_revision_id": "revision-1",
                    "generation_batch_id": "batch-1",
                    "title": "server title",
                    "prompt": "server prompt",
                }
            ],
        }

    def record_exposure(
        self,
        event: dict[str, Any],
        *,
        created_at: float | None = None,
    ) -> bool:
        self.exposure_calls.append(event)
        if event != {
            "session_id": "visit-session",
            "slate_id": "exposure-1",
            "manifest_id": "manifest-1",
            "generation_batch_id": "batch-1",
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "exposure_slice_id": "manifest-1:window:0",
            "topics": [{"card_revision_id": "revision-1"}],
        }:
            return False
        return self.store.record_event(
            _channel_event(
                event_type="dynamic_exposure",
                slate_id="exposure-1",
                topic_id="manifest-1",
                payload={
                    "manifest_id": "manifest-1",
                    "generation_batch_id": "batch-1",
                    "exposure_id": "exposure-1",
                    "card_revision_ids": ["revision-1"],
                    "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                    "project_id": None,
                    "provider_response_ids_by_card_revision": {
                        "revision-1": list(CHANNEL_PROVIDER_RESPONSE_IDS)
                    },
                    "search_receipts_by_card_revision": {
                        "revision-1": CHANNEL_SEARCH_RECEIPT
                    },
                },
            )
        )

    def schedule_successor(
        self,
        *,
        context_scope: str,
        project_id: str | None,
        visit_id: str,
        work_mode: str = "work",
    ) -> None:
        del work_mode
        self.successor_calls.append((context_scope, project_id, visit_id))

def _install_manifest_and_card(store: TopicStarterStore) -> None:
    receipts = {
        component_id: {
            "binding": {"provider_version": "doubao-seed-test"},
            "request_id": request_id,
        }
        for component_id, request_id in CHANNEL_REQUEST_IDS
    }
    assert store.record_event(
        _channel_event(
            event_type="starter_slate_manifest",
            slate_id="manifest-1",
            topic_id="manifest-1",
            payload={
                "manifest_id": "manifest-1",
                "generation_batch_id": "batch-1",
                "card_revision_ids": ["revision-1"],
                "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                "project_id": None,
            },
        )
    )
    assert store.record_event(
        _channel_event(
            event_type="starter_card",
            slate_id="manifest-1",
            topic_id="revision-1",
            payload={
                "manifest_id": "manifest-1",
                "generation_batch_id": "batch-1",
                "card_revision_id": "revision-1",
                "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                "project_id": None,
                "intent": "causal recommendation evaluation",
                "title": "How should causal recommendation be evaluated?",
                "prompt": "Compare causal recommendation evaluation protocols",
                "source_memory_unit_ids": ["memory-causal-evaluation"],
                "component_receipts": receipts,
                "component_invocation_receipts": [
                    {
                        "component_id": component_id,
                        "request_id": request_id,
                    }
                    for component_id, request_id in CHANNEL_REQUEST_IDS
                ],
                "search_provider_id": "ark-responses-web-search",
                "search_receipt": CHANNEL_SEARCH_RECEIPT,
                "provider_invocation_receipts": [
                    {"provider_response_id": response_id}
                    for response_id in CHANNEL_PROVIDER_RESPONSE_IDS
                ],
            },
        )
    )

def _ui_event(
    *,
    event_type: str,
    batch_id: str = "batch-1",
    session_id: str = "visit-session",
) -> dict[str, Any]:
    card_event_types = {"topic_click", "session_bound"}
    return {
        "session_id": session_id,
        "slate_id": "exposure-1",
        "event_type": event_type,
        "topic_id": "revision-1" if event_type in card_event_types else None,
        "profile_version": 3,
        "policy_id": POLICY_ID,
        "selection_mode": "personalized_dynamic",
        "propensity": None,
        "propensity_reason": "not_estimated",
        "payload": {
            "manifest_id": "manifest-1",
            "generation_batch_id": batch_id,
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "short_intent_version": 1,
            "exposure_slice_id": "manifest-1:window:0",
            **(
                {"topics": [{"card_revision_id": "revision-1"}]}
                if event_type == "dynamic_exposure"
                else (
                    {"card_revision_id": "revision-1"}
                    if event_type in card_event_types
                    else {}
                )
            ),
        },
    }

def _register_panel(channel, *, service, store, queue) -> None:
    """Register the panel methods against an AgentServer that owns the stub pipeline."""
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_to_agent_response
    from jiuwenswarm.common.e2a.models import E2AResponse
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.recommendation_host import RecommendationHost

    host = RecommendationHost()
    host.store = store
    host.topic_service = service
    host.topic_outcome_queue = queue
    host._services_built = True
    server = AgentWebSocketServer()
    host.bind_push(server._push_recommendation_event)
    server._recommendation_host = host

    class _Socket:
        remote_address = ("127.0.0.1", 12345)

        def __init__(self) -> None:
            self.frames: list[dict[str, Any]] = []

        async def send(self, data: str | bytes) -> None:
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            self.frames.append(json.loads(data))

    class _Client:
        async def send_request(self, envelope):
            socket = _Socket()
            await server.dispatch_parsed_request(
                socket, e2a_to_agent_request(envelope), asyncio.Lock(),
            )
            return e2a_response_to_agent_response(E2AResponse.from_dict(socket.frames[-1]))

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=_Client()))


@pytest.mark.asyncio
async def test_webchannel_scopes_exposure_and_server_derived_outcomes(
    tmp_path: Any,
    enabled_recommendation_consent,
) -> None:
    store = TopicStarterStore(tmp_path / "wiring.sqlite3")
    _install_manifest_and_card(store)
    service = _TopicServiceStub(store)
    queue = NewPageOutcomeQueue()
    assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == []
    channel = WebChannel(
        WebChannelConfig(enabled=True), RobotMessageRouter()
    )
    channel.on_message(lambda _msg: False)
    _register_panel(channel, service=service, store=store, queue=queue)
    ws = _OpenWebSocket("/ws?user_id=alice&session_id=visit-session")
    connection = asyncio.create_task(channel.handle_connection(ws, path=ws.path))

    ws.request(
        "get-v6",
        "topic_starters.get",
        {
            "session_id": "visit-session",
            "policy_id": POLICY_ID,
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
        },
    )
    response = await _wait_response(ws, "get-v6")
    assert response["ok"] is True
    assert response["payload"]["policy_id"] == POLICY_ID
    assert service.serve_calls == [
        ("visit-session", GLOBAL_NEW_CHAT_SCOPE, None)
    ]

    ws.request("exposure", "topic_starters.event", _ui_event(event_type="dynamic_exposure"))
    assert (await _wait_response(ws, "exposure"))["payload"] == {"recorded": True}

    ws.request("accepted", "topic_starters.event", _ui_event(event_type="topic_click"))
    assert (await _wait_response(ws, "accepted"))["payload"] == {"recorded": True}
    assert service.successor_calls == []
    ws.request(
        "session-bound",
        "topic_starters.event",
        _ui_event(event_type="session_bound", session_id="real-chat-session"),
    )
    assert (await _wait_response(ws, "session-bound"))["payload"] == {
        "recorded": True
    }
    ws.request("ignored", "topic_starters.event", _ui_event(event_type="not_interested"))
    assert (await _wait_response(ws, "ignored"))["payload"] == {"recorded": False}
    ws.request(
        "direct",
        "topic_starters.event",
        _ui_event(event_type="post_exposure_direct"),
    )
    assert (await _wait_response(ws, "direct"))["payload"] == {"recorded": True}
    ws.request(
        "page-left",
        "topic_starters.event",
        _ui_event(event_type="page_left"),
    )
    assert (await _wait_response(ws, "page-left"))["payload"] == {
        "recorded": True
    }
    assert service.successor_calls == []

    stored = store.query_events(
        event_type="topic_click",
        slate_id="exposure-1",
        topic_id="revision-1",
        policy_id=POLICY_ID,
    )
    assert len(stored) == 1
    assert stored[0]["payload"]["manifest_id"] == "manifest-1"
    assert stored[0]["payload"]["generation_batch_id"] == "batch-1"
    assert store.query_events(
        event_type="not_interested",
        slate_id="exposure-1",
        topic_id="revision-1",
        policy_id=POLICY_ID,
    ) == []

    bound = store.query_events(
        event_type="session_bound",
        slate_id="exposure-1",
        topic_id="revision-1",
        policy_id=POLICY_ID,
    )
    assert len(bound) == 1
    assert bound[0]["session_id"] == "real-chat-session"
    assert bound[0]["payload"]["started_session_id"] == "real-chat-session"

    for event_type in ("post_exposure_direct", "page_left"):
        stored = store.query_events(
            event_type=event_type,
            slate_id="exposure-1",
            topic_id="manifest-1",
            policy_id=POLICY_ID,
        )
        assert len(stored) == 1
        assert stored[0]["payload"] == {
            "manifest_id": "manifest-1",
            "generation_batch_id": "batch-1",
            "exposure_id": "exposure-1",
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
        }

    ws.request(
        "tampered",
        "topic_starters.event",
        _ui_event(event_type="topic_click", batch_id="tampered"),
    )
    assert (await _wait_response(ws, "tampered"))["payload"] == {
        "recorded": False
    }
    assert len(service.successor_calls) == 0
    ws.request(
        "tampered-direct",
        "topic_starters.event",
        _ui_event(event_type="post_exposure_direct", batch_id="tampered"),
    )
    assert (await _wait_response(ws, "tampered-direct"))["payload"] == {
        "recorded": False
    }

    ws.release()
    await asyncio.wait_for(connection, timeout=5.0)

@pytest.mark.asyncio
async def test_webchannel_rebuilds_product_lane_when_free_search_is_off(
    enabled_recommendation_consent,
    monkeypatch,
    tmp_path,
) -> None:
    """Free-search toggles must not leave the homepage without a product lane."""

    monkeypatch.setenv("FREE_SEARCH_DDG_ENABLED", "false")
    monkeypatch.setenv("FREE_SEARCH_BING_ENABLED", "false")
    channel = WebChannel(
        WebChannelConfig(enabled=True), RobotMessageRouter()
    )
    channel.on_message(lambda _msg: False)
    store = TopicStarterStore(tmp_path / "webchannel-free-search-off.db")
    # Simulate a prior build that left the product lane empty; ensure_services
    # must recreate it even while both free-search engines are off.
    _register_panel(channel, service=None, store=store, queue=None)
    ws = _OpenWebSocket("/ws?user_id=alice&session_id=visit-session")
    connection = asyncio.create_task(channel.handle_connection(ws, path=ws.path))

    ws.request(
        "get-presets",
        "topic_starters.get",
        {
            "session_id": "visit-session",
            "policy_id": POLICY_ID,
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
        },
    )

    response = await _wait_response(ws, "get-presets")
    assert response["ok"] is True
    payload = response["payload"]
    assert payload["available"] is True
    assert payload["session_id"] == "visit-session"
    assert payload["context_scope"] == GLOBAL_NEW_CHAT_SCOPE
    assert 1 <= len(payload.get("topics") or []) <= 4

    ws.release()
    await asyncio.wait_for(connection, timeout=5.0)

def _save_profile_with_dismissals(store: TopicStarterStore, times: tuple[float, ...]) -> None:
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    state = history.empty_history_state()
    state["memory"]["units"] = [{
        "core": {"preference_summary": "因果推荐的评估方法"},
        "companion": {
            "unit_id": "memory-causal-evaluation",
            "canonical_source_locators": [["session-a", 0]],
            "evidence_ids": ["session-a:req-0:user"],
        },
    }]
    state["memory_sidecar"] = {
        "negative_preference_version": len(times),
        "negative_preferences": [{
            "context_scope": GLOBAL_NEW_CHAT_SCOPE, "project_id": None, "direction": "causal evaluation",
            "source_id": "memory-causal-evaluation", "aliases": [], "anchors": [],
            "reason": "explicit_not_interested", "dismissed_at": at, "evidence_session_ids": ["session-a"],
        } for at in times],
    }
    history.save_history_state(store, state, now=CHANNEL_NOW)

@pytest.mark.asyncio
async def test_not_interested_event_is_rejected_without_refill(
    tmp_path: Any,
    enabled_recommendation_consent,
) -> None:
    store = TopicStarterStore(tmp_path / "dismiss.sqlite3")
    _install_manifest_and_card(store)
    _save_profile_with_dismissals(store, (CHANNEL_NOW - 200 * 86_400.0, CHANNEL_NOW - 100 * 86_400.0))
    service = _TopicServiceStub(store)
    queue = NewPageOutcomeQueue()
    assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == []
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.on_message(lambda _msg: False)
    _register_panel(channel, service=service, store=store, queue=queue)
    ws = _OpenWebSocket("/ws?user_id=alice&session_id=visit-session")
    connection = asyncio.create_task(channel.handle_connection(ws, path=ws.path))

    ws.request("exposure", "topic_starters.event", _ui_event(event_type="dynamic_exposure"))
    assert (await _wait_response(ws, "exposure"))["payload"] == {"recorded": True}
    ws.request("dismiss", "topic_starters.event", _ui_event(event_type="not_interested"))
    assert (await _wait_response(ws, "dismiss"))["payload"] == {"recorded": False}
    assert service.successor_calls == []

    ws.release()
    await asyncio.wait_for(connection, timeout=5.0)

# --- test_topic_startup.py ---

import pytest

from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
    OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_rid import (
    RID_SUMMARY_PAIR_MATCHER_ID,
    RID_SUMMARY_PAIR_MATCHER_VERSION,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_service import (
    TopicService,
)
from jiuwenswarm.server.runtime import recommendation_host

def test_gateway_builds_product_lane_without_free_search_engines(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("TOPIC_STARTER_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("FREE_SEARCH_DDG_ENABLED", raising=False)
    monkeypatch.delenv("FREE_SEARCH_BING_ENABLED", raising=False)
    store = TopicStarterStore(tmp_path / "recommendation.db")

    product_service, outcome_queue = (
        recommendation_host._build_topic_starter_services(store)
    )

    assert isinstance(product_service, TopicService)
    assert product_service._search_provider_id == OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID
    assert outcome_queue.pending_count == 0

@pytest.mark.asyncio
async def test_recommendation_host_start_does_not_seed_pools(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TOPIC_STARTER_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("FREE_SEARCH_DDG_ENABLED", raising=False)
    monkeypatch.delenv("FREE_SEARCH_BING_ENABLED", raising=False)
    host = recommendation_host.RecommendationHost()
    host.store = TopicStarterStore(tmp_path / "no-boot-seed.db")
    seeds: list[str] = []

    await host.start()
    assert host.topic_service is not None
    original = host.topic_service.schedule_seed

    def capture(visit_id: str):
        seeds.append(visit_id)
        return original(visit_id)

    host.topic_service.schedule_seed = capture  # type: ignore[method-assign]
    await host.start()
    assert seeds == []
    await host.shutdown()


def test_chat_completed_refreshes_only_matching_scope(tmp_path, monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        scope_for_project,
    )

    host = recommendation_host.RecommendationHost()
    host.store = TopicStarterStore(tmp_path / "chat-scope.db")
    host.ensure_services()
    assert host.topic_service is not None
    scheduled: list[tuple[str, str | None]] = []

    def capture_successor(**kwargs):
        scheduled.append(
            (str(kwargs.get("context_scope") or ""), kwargs.get("project_id")),
        )

    host.topic_service.schedule_successor = capture_successor  # type: ignore[method-assign]

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {"project_id": "proj-a", "work_mode": "chat"},
    )
    host.on_chat_completed("session-project", "req-project")
    assert scheduled == [(scope_for_project("proj-a"), "proj-a")]

    scheduled.clear()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {"project_id": "default", "work_mode": "chat"},
    )
    host.on_chat_completed("session-global", "req-global")
    assert scheduled == [(GLOBAL_NEW_CHAT_SCOPE, None)]


def test_topic_click_records_without_scheduling_successor(tmp_path) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    store = TopicStarterStore(tmp_path / "click-no-schedule.db")
    service = TopicService(
        store,
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=True,
        now=lambda: 1_700_000_000.0,
    )
    scheduled: list[str] = []

    def capture_successor(**kwargs):
        scheduled.append(str(kwargs.get("visit_id") or ""))

    service.schedule_successor = capture_successor  # type: ignore[method-assign]
    served = service.serve(
        "session-new",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
    )
    assert served["available"] is True
    topic = served["topics"][0]
    card_revision_id = str(topic.get("card_revision_id") or topic["id"])
    assert service.record_exposure(
        {
            "session_id": "session-new",
            "slate_id": served["slate_id"],
            "manifest_id": served["manifest_id"],
            "generation_batch_id": served["generation_batch_id"],
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "exposure_slice_id": served["exposure_slice_id"],
            "topics": [
                {
                    "card_revision_id": str(
                        item.get("card_revision_id") or item["id"],
                    ),
                }
                for item in served["topics"]
            ],
        },
        created_at=1_700_000_001.0,
    )
    queue = NewPageOutcomeQueue()
    assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == []
    recorded = service.record_interaction(
        {
            "session_id": "session-new",
            "slate_id": served["slate_id"],
            "event_type": "topic_click",
            "topic_id": card_revision_id,
            "payload": {
                "manifest_id": served["manifest_id"],
                "generation_batch_id": served["generation_batch_id"],
                "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                "project_id": None,
                "card_revision_id": card_revision_id,
            },
        },
        outcome_queue=queue,
        created_at=1_700_000_002.0,
    )
    assert recorded == {"recorded": True}
    assert scheduled == []


def test_gateway_builds_product_lane_when_native_search_chain_is_configured(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("TOPIC_STARTER_SEARCH_PROVIDER", "openjiuwen-native")
    monkeypatch.setenv("FREE_SEARCH_DDG_ENABLED", "true")
    monkeypatch.setenv("FREE_SEARCH_BING_ENABLED", "true")
    store = TopicStarterStore(tmp_path / "recommendation.db")

    product_service, _outcome_queue = (
        recommendation_host._build_topic_starter_services(store)
    )

    assert isinstance(product_service, TopicService)
    assert product_service._search_provider_id == OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID

def test_recommendation_host_builds_once_even_when_free_search_is_off(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.delenv("TOPIC_STARTER_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TOPIC_STARTER_V6_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("FREE_SEARCH_DDG_ENABLED", "false")
    monkeypatch.setenv("FREE_SEARCH_BING_ENABLED", "false")
    host = recommendation_host.RecommendationHost()
    host.store = TopicStarterStore(tmp_path / "recovery.db")
    host.ensure_services()
    assert isinstance(host.topic_service, TopicService)
    queue = host.topic_outcome_queue
    service = host.topic_service
    assert host.topic_service.on_prepared is not None

    monkeypatch.setenv("FREE_SEARCH_DDG_ENABLED", "true")
    host.ensure_services()
    assert host.topic_service is service
    monkeypatch.setenv("FREE_SEARCH_BING_ENABLED", "true")
    host.ensure_services()
    assert host.topic_service is service
    assert host.topic_outcome_queue is queue


def test_cold_start_serve_shows_corpus_cards_without_history(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation.db"),
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=True,
        now=lambda: 1_700_000_000.0,
    )
    try:
        response = service.serve(
            "session-new",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert response["available"] is True
        assert response["work_mode"] == "work"
        assert 1 <= len(response["topics"]) <= 4
        assert response["selection_mode"] == "generic_dynamic"
        assert all(topic["title"] and topic["prompt"] for topic in response["topics"])
    finally:
        pass


def test_topic_pools_isolate_by_work_mode(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        INPUT_REVISION_RECEIPT_VERSION,
        TERMINAL_COMPONENT_IDS,
        build_generation_trigger_key,
        build_serving_compatibility_key,
        normalize_topic_work_mode,
        serving_compatibility_keys_match,
        topic_slot_key,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
        TERMINAL_CONTRACT_VERSION,
    )

    assert normalize_topic_work_mode("Design") == "design"
    assert topic_slot_key(GLOBAL_NEW_CHAT_SCOPE, "code") != topic_slot_key(
        GLOBAL_NEW_CHAT_SCOPE, "work",
    )
    receipt = {
        "receipt_version": INPUT_REVISION_RECEIPT_VERSION,
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "revision_digest": "digest",
    }
    common = dict(
        local_date="2026-11-07",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
        profile_semantic_version=0,
        short_intent_version=0,
        negative_preference_version=0,
        component_contract_versions={
            component_id: TERMINAL_CONTRACT_VERSION
            for component_id in TERMINAL_COMPONENT_IDS
        },
        model="test",
        generation_config_version="cfg",
        corpus_version="corpus",
        document_snapshot_version="docs",
        retrieval_config_version="retrieval",
        evidence_extractor_version="evidence",
        search_provider_id="test",
        input_revision_receipt=receipt,
    )
    work_key = build_generation_trigger_key(**common, work_mode="work")
    design_key = build_generation_trigger_key(**common, work_mode="design")
    assert work_key["work_mode"] == "work"
    assert design_key["work_mode"] == "design"
    assert build_serving_compatibility_key(work_key) != build_serving_compatibility_key(
        design_key
    )
    other_model = build_generation_trigger_key(**{**common, "model": "other"}, work_mode="work")
    other_endpoint = build_generation_trigger_key(
        **{**common, "model": "other", "generation_config_version": "cfg:base_url=https://other.example"},
        work_mode="work",
    )
    serving = build_serving_compatibility_key(work_key)
    assert "model" not in serving
    assert "generation_config_version" not in serving
    assert serving_compatibility_keys_match(
        {**serving, "model": "legacy"},
        build_serving_compatibility_key(other_model),
    )
    assert serving_compatibility_keys_match(
        serving,
        build_serving_compatibility_key(other_endpoint),
    )

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation.db"),
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: 1_700_000_000.0,
    )
    work = service.serve(
        "session-work",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
        work_mode="work",
    )
    design = service.serve(
        "session-design",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
        work_mode="design",
    )
    code = service.serve(
        "session-code",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
        work_mode="code",
    )
    assert work["available"] is True
    assert work["work_mode"] == "work"
    # Packaged corpus stand-ins are work-only; design/code wait for personalized pools.
    assert design["available"] is False
    assert design["work_mode"] == "design"
    assert design.get("topics") in (None, [])
    assert code["available"] is False
    assert code["work_mode"] == "code"
    assert code.get("topics") in (None, [])


@pytest.mark.asyncio
async def test_topic_web_search_is_optional_enrichment() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        TopicPipeline,
        _FrozenEvidenceBatch,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
        topic_web_search_engines_enabled,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
        TopicStarterStore,
    )

    assert topic_web_search_engines_enabled(environ={}) is False
    assert topic_web_search_engines_enabled(
        environ={"FREE_SEARCH_DDG_ENABLED": "true"},
    ) is True
    assert topic_web_search_engines_enabled(
        environ={"FREE_SEARCH_BING_ENABLED": "1"},
    ) is True

    pipeline = TopicPipeline(TopicStarterStore(":memory:"))
    trigger = {
        "local_date": "2026-11-07",
        "document_snapshot_version": "docs:v1",
    }
    host_only = await pipeline._freeze_documents(
        [
            {
                "candidate_id": "host-1",
                "profile_evidence": {
                    "source_gate_mode": "profile_interest_exact_summary",
                },
            }
        ],
        snapshot=object(),
        trigger_key=trigger,
        external_audit=object(),
        search_provider=None,
    )
    assert isinstance(host_only, _FrozenEvidenceBatch)
    assert host_only.frozen is None
    assert host_only.rejections == {}

    rejected = await pipeline._freeze_documents(
        [{"candidate_id": "web-1", "title": "t"}],
        snapshot=object(),
        trigger_key=trigger,
        external_audit=object(),
        search_provider=None,
    )
    assert rejected.rejections["web-1"] == (
        "search",
        "web_search_provider_unavailable",
    )


def test_empty_personalized_pool_still_serves_presets_when_provider_refused(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
        PROVIDER_INVOCATION_EVENT_TYPE,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        POLICY_ID,
    )

    store = TopicStarterStore(tmp_path / "recommendation.db")
    store.record_event({
        "session_id": "usage",
        "slate_id": "usage",
        "event_type": PROVIDER_INVOCATION_EVENT_TYPE,
        "topic_id": "request_failed",
        "policy_id": "topic-starter-provider-invocation-v1",
        "selection_mode": "provider_invocation",
        "propensity_reason": "not_estimated",
        "payload": {
            "request_id": "req-1",
            "attempt_id": "attempt-1",
            "status": "request_failed",
            "model": "test",
            "failure_reason": "invalid_key",
        },
        "created_at": 1_700_000_000.0,
    })
    store.record_event({
        "session_id": "topic-starter-background",
        "slate_id": "attempt",
        "event_type": "background_generation_failed",
        "topic_id": "attempt",
        "policy_id": POLICY_ID,
        "selection_mode": "background_generation",
        "propensity_reason": "not_estimated",
        "payload": {
            "reason": "provider_request_failed",
            "generation_trigger_key": {
                "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                "work_mode": "work",
                "model": "test",
            },
        },
        "created_at": 1_700_000_000.0,
    })
    service = TopicService(
        store,
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=True,
        now=lambda: 1_700_000_000.0,
    )
    assert service.configuration_status()["provider_refused"] is True
    assert service.preparation_state(GLOBAL_NEW_CHAT_SCOPE) == "provider_unavailable"
    response = service.serve(
        "session-new",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
    )
    assert response["available"] is True
    assert 1 <= len(response["topics"]) <= 4
    assert response["selection_mode"] == "generic_dynamic"
    assert response["preparation_state"] == "provider_unavailable"


@pytest.mark.asyncio
async def test_no_content_stand_in_presets_do_not_self_schedule(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        POLICY_ID,
    )

    started = asyncio.Event()

    async def generate(_trigger, _config, _scope, _project_id, _visit_id):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("serve must not start generation from presets alone")

    store = TopicStarterStore(tmp_path / "recommendation.db")
    store.record_event({
        "session_id": "topic-starter-background",
        "slate_id": "attempt",
        "event_type": "background_generation_abstained",
        "topic_id": "attempt",
        "policy_id": POLICY_ID,
        "selection_mode": "background_generation",
        "propensity_reason": "not_estimated",
        "payload": {
            "reason": "no_eligible_history",
            "generation_trigger_key": {
                "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                "work_mode": "work",
                "model": "test",
            },
        },
        "created_at": 1_700_000_000.0,
    })
    service = TopicService(
        store,
        generate=generate,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=True,
        now=lambda: 1_700_000_001.0,
    )
    try:
        assert service.preparation_state(GLOBAL_NEW_CHAT_SCOPE) == "no_content"
        response = service.serve(
            "session-new",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert response["available"] is True
        assert response["topics"]
        assert response["selection_mode"] == "generic_dynamic"
        assert response.get("preparation_state") != "preparing"
        await asyncio.sleep(0.05)
        assert not started.is_set()
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_reload_model_keeps_preset_cards_without_preparing(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    configs = [
        ArkBackgroundConfig(api_key="test", model="old-model", context_window_tokens=131072),
        ArkBackgroundConfig(api_key="test", model="new-model", context_window_tokens=131072),
    ]
    started = asyncio.Event()
    successors: list[str] = []

    async def generate(_trigger, _config, _scope, _project_id, _visit_id):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("model change must not schedule generation")

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation-model-quiet.db"),
        generate=generate,
        search_provider_id="test",
        config_loader=lambda: configs[0],
        enabled=True,
        now=lambda: 1_700_000_000.0,
    )
    original_successor = service.schedule_successor

    def capture_successor(**kwargs):
        successors.append(str(kwargs.get("visit_id") or ""))
        return original_successor(**kwargs)

    service.schedule_successor = capture_successor  # type: ignore[method-assign]
    try:
        before = service.serve(
            "session-before",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert before["available"] is True
        assert before["topics"]
        before_ids = [topic["id"] for topic in before["topics"]]
        configs[0] = configs[1]
        await service.reload_config()
        after = service.serve(
            "session-after",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert after["available"] is True
        assert [topic["id"] for topic in after["topics"]] == before_ids
        assert after.get("preparation_state") != "preparing"
        await asyncio.sleep(0.05)
        assert not started.is_set()
        assert successors == []
    finally:
        await service.shutdown()


def test_disabled_serve_still_returns_preset_cards(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation-disabled.db"),
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: 1_700_000_000.0,
    )
    response = service.serve(
        "session-disabled",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
    )
    assert response["available"] is True
    assert response["preparation_state"] == "disabled"
    assert 1 <= len(response["topics"]) <= 4


def test_disabled_project_serve_still_returns_preset_cards(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        scope_for_project,
    )

    project_id = "proj_preset_align"
    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation-disabled-project.db"),
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: 1_700_000_000.0,
    )
    response = service.serve(
        "session-disabled-project",
        context_scope=scope_for_project(project_id),
        project_id=project_id,
    )
    assert response["available"] is True
    assert response["preparation_state"] == "disabled"
    assert response["context_scope"] == scope_for_project(project_id)
    assert response["project_id"] == project_id
    assert 1 <= len(response["topics"]) <= 4
    assert all(topic["title"] and topic["prompt"] for topic in response["topics"])


def test_empty_project_pool_still_serves_presets_when_enabled(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        scope_for_project,
    )

    project_id = "proj_empty_preset"
    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation-empty-project.db"),
        # No generator: stand-in must still fill the project welcome page.
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=True,
        now=lambda: 1_700_000_000.0,
    )
    response = service.serve(
        "session-empty-project",
        context_scope=scope_for_project(project_id),
        project_id=project_id,
    )
    assert response["available"] is True
    assert response["context_scope"] == scope_for_project(project_id)
    assert response["project_id"] == project_id
    assert 1 <= len(response["topics"]) <= 4
    assert response["selection_mode"] == "generic_dynamic"
    assert all(topic["title"] and topic["prompt"] for topic in response["topics"])


def test_hot_list_open_url_prefers_topic_page_never_board():
    from jiuwenswarm.agents.harness.common.recommendation.topic_news import (
        BAIDU_PROVIDER_ID,
        BAIDU_REALTIME_URL,
        WEIBO_HOT_BAND_URL,
        WEIBO_HOT_LIST_PAGE_URL,
        WEIBO_PROVIDER_ID,
        HotListEntry,
        HotListPull,
        hot_list_open_url,
        hot_list_topic_url,
        parse_weibo_hot_band,
    )

    pull = HotListPull(
        receipt_id="pull-1",
        provider_id=WEIBO_PROVIDER_ID,
        list_url=WEIBO_HOT_BAND_URL,
        pulled_at=1.0,
        entries=(),
    )
    with_page = HotListEntry(
        entry_id="weibo-hot-band:1",
        provider_id=WEIBO_PROVIDER_ID,
        rank=1,
        word="甲",
        page_url="https://s.weibo.com/weibo?q=%23甲%23",
    )
    without_page = HotListEntry(
        entry_id="weibo-hot-band:2",
        provider_id=WEIBO_PROVIDER_ID,
        rank=2,
        word="乙",
    )
    assert hot_list_open_url(with_page, pull) == with_page.page_url
    assert hot_list_open_url(without_page, pull) == hot_list_topic_url(without_page)
    assert hot_list_open_url(without_page, pull) != WEIBO_HOT_LIST_PAGE_URL
    baidu_pull = HotListPull(
        receipt_id="pull-2",
        provider_id=BAIDU_PROVIDER_ID,
        list_url=BAIDU_REALTIME_URL,
        pulled_at=1.0,
        entries=(),
    )
    baidu = HotListEntry(
        entry_id="baidu-realtime:1",
        provider_id=BAIDU_PROVIDER_ID,
        rank=1,
        word="丙",
    )
    assert hot_list_open_url(baidu, baidu_pull) == hot_list_topic_url(baidu)
    assert hot_list_open_url(baidu, baidu_pull) != BAIDU_REALTIME_URL
    band = parse_weibo_hot_band(
        '{"data":{"band_list":[{"word":"丁","realpos":1,"is_ad":0},'
        '{"word":"戊","realpos":2,"url":"https://s.weibo.com/weibo?q=%23戊%23","is_ad":0}]}}'
    )
    assert band[0].page_url == hot_list_topic_url(band[0])
    assert "%23" in band[0].page_url
    assert band[1].page_url == "https://s.weibo.com/weibo?q=%23戊%23"


def test_corpus_avoids_anecdote_hunting_starters():
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        _CARD_TITLE_MAX_CHARS,
        card_copy_rejection_reason,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
        CORPUS_VERSION,
        load_fixed_starter_corpus,
    )

    for starter in load_fixed_starter_corpus(CORPUS_VERSION):
        assert card_copy_rejection_reason(starter.text, max_chars=_CARD_TITLE_MAX_CHARS) is None
        assert "哪一次迷路" not in starter.text


def test_card_copy_allows_abbreviation_periods_in_a_question():
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        card_copy_rejection_reason,
    )

    assert card_copy_rejection_reason(
        "What voting rights do Washington D.C. residents have?"
    ) is None
    assert card_copy_rejection_reason(
        "How does U.S. federalism work?"
    ) is None
    assert card_copy_rejection_reason(
        "First sentence. Second question?"
    ) == "multiple_sentences"


def test_corpus_versions_select_language_files():
    from collections import Counter

    from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
        CORPUS_VERSION_EN,
        CORPUS_VERSION_ZH,
        load_fixed_starter_corpus,
    )

    zh = load_fixed_starter_corpus(CORPUS_VERSION_ZH)
    en = load_fixed_starter_corpus(CORPUS_VERSION_EN)

    assert len(zh) == len(en) == 100
    for rows in (zh, en):
        counts = Counter(starter.topic for starter in rows)
        assert set(counts) == {"生活", "科学", "文化", "实用", "好奇"}
        assert all(count == 20 for count in counts.values())
    assert all(
        not any("一" <= ch <= "鿿" for ch in starter.text) for starter in en
    )
    assert any("一" <= ch <= "鿿" for ch in zh[0].text)

    removed = {
        "culture-022", "practical-021", "curiosity-021",
        "life-016", "life-020", "science-006", "science-015",
        "culture-014", "practical-014", "curiosity-007",
    }
    assert not removed & {starter.starter_id for starter in zh}


def test_cold_start_cards_follow_language():
    from datetime import datetime, timezone

    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        build_cold_start_cards,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
        CORPUS_VERSION_EN,
        CORPUS_VERSION_ZH,
    )

    now = datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp()

    def trigger(version):
        return {
            "local_date": "2026-11-16",
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "profile_semantic_version": 0,
            "short_intent_version": 0,
            "search_provider_id": "test",
            "generation_config_version": "test",
            "corpus_version": version,
            "document_snapshot_version": "documents:2026-11-16:v2",
            "retrieval_config_version": "test",
            "evidence_extractor_version": "test",
            "input_revision_receipt": {"receipt_id": "r1"},
        }

    def has_cjk(text):
        return any("一" <= ch <= "鿿" for ch in text)

    en_cards = build_cold_start_cards(
        trigger_key=trigger(CORPUS_VERSION_EN), now=now, target_count=4,
    )
    zh_cards = build_cold_start_cards(
        trigger_key=trigger(CORPUS_VERSION_ZH), now=now, target_count=4,
    )

    assert len(en_cards) == 4
    assert all(not has_cjk(card["title"] + card["prompt"]) for card in en_cards)
    assert len(zh_cards) == 4
    assert any(has_cjk(card["title"]) for card in zh_cards)


def test_generated_stage_prompts_carry_output_language():
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _v6_system_prompt,
    )

    english = _v6_system_prompt("v6_global_interest_generator", "base", "en")
    chinese = _v6_system_prompt("v6_global_interest_generator", "base", "zh")
    scorer = _v6_system_prompt("v6_utility_scorer", "base", "en")

    assert "OUTPUT LANGUAGE" in english and "English" in english
    assert "OUTPUT LANGUAGE" in chinese and "Chinese" in chinese
    assert "OUTPUT LANGUAGE" not in scorer


def test_candidates_envelope_accepts_a_bare_array():
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _candidates_envelope,
    )

    items = [{"slot_id": "s1"}]

    assert _candidates_envelope(items) == {"candidates": items}
    assert _candidates_envelope({"candidates": items}) == {"candidates": items}
    assert _candidates_envelope("not-json-shape") == "not-json-shape"


def test_language_change_invalidates_pool_identity():
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        build_serving_compatibility_key,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
        CORPUS_VERSION_EN,
        CORPUS_VERSION_ZH,
        corpus_version_for_language,
    )

    assert corpus_version_for_language("en") == CORPUS_VERSION_EN
    assert corpus_version_for_language("EN") == CORPUS_VERSION_EN
    assert corpus_version_for_language("zh") == CORPUS_VERSION_ZH
    assert corpus_version_for_language("fr") == CORPUS_VERSION_ZH
    assert build_serving_compatibility_key(
        {"corpus_version": CORPUS_VERSION_EN}
    ) != build_serving_compatibility_key({"corpus_version": CORPUS_VERSION_ZH})


def test_recommendation_language_normalizes_preferred_language(monkeypatch):
    from jiuwenswarm.agents.harness.common.recommendation import topic_controls

    for raw, expected in (
        ("en", "en"), ("EN", "en"), ("zh", "zh"), ("fr", "zh"), (None, "zh"),
    ):
        monkeypatch.setattr(
            topic_controls, "get_config", lambda _raw=raw: {"preferred_language": _raw},
        )
        topic_controls.clear_recommendation_language_cache()
        assert topic_controls.recommendation_language() == expected


def test_deprojected_profile_uses_english_taxonomy():
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _deprojected_long_profile,
    )

    state = {
        "memory": {
            "units": [
                {
                    "core": {"behavior_pattern": "tech-ai-ml"},
                    "companion": {"unit_id": "unit-1"},
                }
            ]
        }
    }

    english = _deprojected_long_profile(
        state,
        profile_semantic_version=1,
        project_by_session={},
        project_identifiers_by_session={},
        language="en",
    )
    chinese = _deprojected_long_profile(
        state,
        profile_semantic_version=1,
        project_by_session={},
        project_identifiers_by_session={},
        language="zh",
    )

    assert english[0].summary_text == "AI and machine learning"
    assert chinese[0].summary_text == "人工智能与机器学习"


def test_hot_list_templates_follow_language():
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _has_cjk,
        _hot_list_title,
    )

    assert _hot_list_title("topic", "en").startswith("What is")
    assert "热搜" in _hot_list_title("话题", "zh")
    assert _has_cjk("话题") is True
    assert _has_cjk("topic") is False


def test_cold_start_prefers_live_timely_corpus_cards():
    from datetime import datetime, timezone

    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        build_cold_start_cards,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
        CORPUS_VERSION,
    )

    now = datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp()
    cards = build_cold_start_cards(
        trigger_key={
            "local_date": "2026-10-21",
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "profile_semantic_version": 0,
            "short_intent_version": 0,
            "search_provider_id": "test",
            "generation_config_version": "test",
            "corpus_version": CORPUS_VERSION,
            "document_snapshot_version": "documents:2026-10-21:v2",
            "retrieval_config_version": "test",
            "evidence_extractor_version": "test",
            "input_revision_receipt": {"receipt_id": "r1"},
        },
        now=now,
    )
    timely_ids = {
        "life-021",
        "life-022",
        "science-021",
        "science-022",
        "culture-021",
        "culture-022",
        "practical-021",
        "practical-022",
        "curiosity-021",
        "curiosity-022",
    }
    assert cards
    assert any(card.get("source_starter_id") in timely_ids for card in cards)


def test_preset_cards_serve_when_recommendation_is_off(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation.db"),
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: 1_700_000_000.0,
    )
    response = service.serve(
        "session-new",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
    )
    assert response["available"] is True
    assert response["preparation_state"] == "disabled"
    assert 1 <= len(response["topics"]) <= 4
    assert response["selection_mode"] == "generic_dynamic"
    assert all(topic["title"] and topic["prompt"] for topic in response["topics"])


def test_product_rejects_not_interested_without_scheduling(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
        NewPageOutcomeQueue,
        TopicStarterStoreOutcomeSink,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    store = TopicStarterStore(tmp_path / "recommendation.db")
    scheduled: list[str] = []
    service = TopicService(
        store,
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: 1_700_000_000.0,
    )
    service.schedule_successor = (  # type: ignore[method-assign]
        lambda **kwargs: scheduled.append(str(kwargs.get("visit_id") or ""))
    )
    first = service.serve(
        "session-new",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
    )
    topic = first["topics"][0]
    queue = NewPageOutcomeQueue()
    assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == []
    reply = service.record_interaction(
        {
            "session_id": "session-new",
            "slate_id": first["slate_id"],
            "event_type": "not_interested",
            "topic_id": topic["id"],
            "payload": {
                "manifest_id": first["manifest_id"],
                "generation_batch_id": first["generation_batch_id"],
                "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                "project_id": None,
                "card_revision_id": topic["id"],
            },
        },
        outcome_queue=queue,
    )
    assert reply == {"recorded": False}
    assert scheduled == []


@pytest.mark.asyncio
async def test_opened_preset_cards_are_resampled_from_the_corpus(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
        NewPageOutcomeQueue,
        TopicStarterStoreOutcomeSink,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    clock = [1_700_000_000.0]
    store = TopicStarterStore(tmp_path / "recommendation.db")
    service = TopicService(
        store,
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: clock[0],
    )
    try:
        first = service.serve(
            "session-new",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert len(first["topics"]) == 4
        assert service.record_exposure({
            "session_id": "session-new",
            "slate_id": first["slate_id"],
            "manifest_id": first["manifest_id"],
            "generation_batch_id": first["generation_batch_id"],
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "exposure_slice_id": first["exposure_slice_id"],
            "topics": [{"card_revision_id": topic["id"]} for topic in first["topics"]],
        }) is True

        queue = NewPageOutcomeQueue()
        assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == []
        opened = first["topics"][:2]
        for topic in opened:
            assert service.record_interaction(
                {
                    "session_id": "session-new",
                    "slate_id": first["slate_id"],
                    "event_type": "topic_click",
                    "topic_id": topic["id"],
                    "payload": {
                        "manifest_id": first["manifest_id"],
                        "generation_batch_id": first["generation_batch_id"],
                        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                        "project_id": None,
                        "card_revision_id": topic["id"],
                    },
                },
                outcome_queue=queue,
            )["recorded"] is True

        # Returning to the welcome page reads without a slate: the two opened
        # preset cards must not linger as a half-empty screen.
        clock[0] += 1.0
        reopened = service.serve(
            "session-new",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert reopened["available"] is True
        assert len(reopened["topics"]) == 4
        opened_starters = {topic["source_starter_id"] for topic in opened}
        assert opened_starters.isdisjoint(
            {topic["source_starter_id"] for topic in reopened["topics"]}
        )
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_turning_recommendation_on_prepares_past_the_preset_pool(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    started = asyncio.Event()

    async def generate(_trigger, _config, _scope, _project_id, _visit_id):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("the preset stand-in run should be cancelled")

    service = TopicService(
        TopicStarterStore(tmp_path / "recommendation.db"),
        generate=generate,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: 1_700_000_000.0,
    )
    try:
        off = service.serve(
            "session-new",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert off["preparation_state"] == "disabled"
        assert off["topics"]
        await service.set_enabled(True, persist=False)
        await asyncio.wait_for(started.wait(), timeout=5)
        on = service.serve(
            "session-new",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert on["preparation_state"] == "preparing"
        assert on["topics"]
        design = service.serve(
            "session-design",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="design",
        )
        assert design.get("preparation_state") != "preparing"
        assert design["available"] is False
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_design_and_code_welcome_skip_packaged_presets(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        build_cold_start_cards,
        write_cold_start_pool,
    )

    clock = [1_700_000_000.0]
    store = TopicStarterStore(tmp_path / "recommendation.db")
    service = TopicService(
        store,
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: clock[0],
    )
    try:
        work = service.serve(
            "session-work",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="work",
        )
        assert work["available"] is True
        assert len(work["topics"]) == 4

        # Even a leftover design cold-start pool must not surface on the design welcome page.
        design_trigger = service._trigger_key(
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="design",
        )
        written = write_cold_start_pool(
            store,
            build_cold_start_cards(trigger_key=design_trigger, now=clock[0]),
            trigger_key=design_trigger,
            created_at=clock[0],
        )
        assert written
        design = service.serve(
            "session-design",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="design",
        )
        code = service.serve(
            "session-code",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="code",
        )
        assert design["available"] is False
        assert code["available"] is False
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_design_welcome_serves_personalized_pool(tmp_path):
    """design/code skip packaged presets and serve a personalized pool only while on."""

    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        LIST_SEARCH_VERSION,
        PAYLOAD_VERSION,
        POLICY_ID,
        PROFILE_INTEREST_RECEIPT_VERSION,
        build_serving_compatibility_key,
    )

    clock = [1_700_000_000.0]
    store = TopicStarterStore(tmp_path / "recommendation.db")
    service = TopicService(
        store,
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: clock[0],
    )
    try:
        trigger = service._trigger_key(
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="design",
        )
        batch_id = "v6-batch-design-personalized"
        manifest_id = "v6-manifest-design-personalized"
        revision_id = "v6-revision-design-personalized"
        snapshot_id = "v6-input:design-personalized"
        title = "Refine this visual system"
        prompt = "Propose three layout refinements for the current design system"
        card = {
            "card_id": "candidate-design-personalized",
            "card_revision_id": revision_id,
            "generation_batch_id": batch_id,
            "selector_rank": 1,
            "category": "profile_interest_continuation",
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "work_mode": "design",
            "interest_summary_id": "long-term-profile:1:unit-0001",
            "source_starter_id": None,
            "intent": title,
            "title": title,
            "prompt": prompt,
            "effective_time": clock[0],
            "expire_time": clock[0] + 7 * 24 * 60 * 60,
            "evidence_bundle": {
                "candidate_id": "candidate-design-personalized",
                "fetch_status": "success",
                "source_url": "profile://interest/unit-0001",
                "source_kind": "authoritative",
                "document_revision_id": "profile:unit-0001",
                "host_receipt": {
                    "receipt_version": PROFILE_INTEREST_RECEIPT_VERSION,
                    "lane": "host_profile_interest_locator",
                    "input_snapshot_id": snapshot_id,
                    "interest_summary_id": "long-term-profile:1:unit-0001",
                },
            },
            "search_provider_id": trigger["search_provider_id"],
            "search_receipt": None,
            "source_memory_unit_ids": ["unit-0001"],
            "source_locators": [],
            "project_source_locators": [],
            "generation_input_snapshot_id": snapshot_id,
            "profile_semantic_version": trigger["profile_semantic_version"],
            "short_intent_version": trigger["short_intent_version"],
            "component_receipts": {},
            "component_invocation_receipts": [],
            "provider_invocation_receipts": [],
            "generation_config_version": trigger["generation_config_version"],
            "list_search_version": LIST_SEARCH_VERSION,
            "policy_id": POLICY_ID,
            "payload_version": PAYLOAD_VERSION,
        }
        manifest = {
            "manifest_id": manifest_id,
            "generation_batch_id": batch_id,
            "card_revision_ids": [revision_id],
            "trigger_origin": "schedule_successor",
            "local_date": trigger["local_date"],
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "work_mode": "design",
            "generation_trigger_key": dict(trigger),
            "generation_input_snapshot_id": snapshot_id,
            "serving_compatibility_key": build_serving_compatibility_key(dict(trigger)),
            "profile_semantic_version": trigger["profile_semantic_version"],
            "short_intent_version": trigger["short_intent_version"],
            "negative_preference_version": trigger["negative_preference_version"],
            "component_receipts": {},
            "generation_config_version": trigger["generation_config_version"],
            "search_provider_id": trigger["search_provider_id"],
            "list_search": {
                "search_version": LIST_SEARCH_VERSION,
                "bounded_beam_trace": {"entries": []},
            },
            "preference_pairs": [],
            "policy_id": POLICY_ID,
            "payload_version": PAYLOAD_VERSION,
        }
        assert store.record_events_atomic(
            [
                {
                    "session_id": "v6-pool",
                    "slate_id": manifest_id,
                    "event_type": "starter_card",
                    "topic_id": revision_id,
                    "position": 1,
                    "profile_version": card["profile_semantic_version"],
                    "policy_id": POLICY_ID,
                    "selection_mode": "pool_generation",
                    "propensity_reason": "not_estimated",
                    "payload": card,
                    "created_at": clock[0],
                },
                {
                    "session_id": "v6-pool",
                    "slate_id": manifest_id,
                    "event_type": "starter_slate_manifest",
                    "topic_id": manifest_id,
                    "profile_version": manifest["profile_semantic_version"],
                    "policy_id": POLICY_ID,
                    "selection_mode": "pool_generation",
                    "propensity_reason": "not_estimated",
                    "payload": manifest,
                    "created_at": clock[0],
                },
            ]
        )
        disabled = service.serve(
            "session-design-personalized",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="design",
        )
        assert disabled["available"] is False
        assert disabled["reason"] == "disabled"

        enabled_service = TopicService(
            store,
            generate=None,
            search_provider_id="test",
            config_loader=lambda: ArkBackgroundConfig(
                api_key="test", model="test", context_window_tokens=131072,
            ),
            enabled=True,
            now=lambda: clock[0],
        )
        design = enabled_service.serve(
            "session-design-personalized",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="design",
        )
        assert design["available"] is True
        assert design["work_mode"] == "design"
        assert len(design["topics"]) >= 1
        assert design["topics"][0]["card_revision_id"] == revision_id
        assert all(
            not topic.get("cold_start") for topic in design["topics"]
        )
        await enabled_service.shutdown()
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_preset_click_does_not_suppress_other_work_modes(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
        NewPageOutcomeQueue,
        TopicStarterStoreOutcomeSink,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    clock = [1_700_000_000.0]
    store = TopicStarterStore(tmp_path / "recommendation.db")
    service = TopicService(
        store,
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=False,
        now=lambda: clock[0],
    )
    try:
        work = service.serve(
            "session-work",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="work",
        )
        assert len(work["topics"]) == 4
        assert service.record_exposure({
            "session_id": "session-work",
            "slate_id": work["slate_id"],
            "manifest_id": work["manifest_id"],
            "generation_batch_id": work["generation_batch_id"],
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "exposure_slice_id": work["exposure_slice_id"],
            "topics": [{"card_revision_id": topic["id"]} for topic in work["topics"]],
        }) is True
        queue = NewPageOutcomeQueue()
        assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == []
        clicked = work["topics"][0]
        assert service.record_interaction(
            {
                "session_id": "session-work",
                "slate_id": work["slate_id"],
                "event_type": "topic_click",
                "topic_id": clicked["id"],
                "payload": {
                    "manifest_id": work["manifest_id"],
                    "generation_batch_id": work["generation_batch_id"],
                    "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                    "project_id": None,
                    "card_revision_id": clicked["id"],
                },
            },
            outcome_queue=queue,
        )["recorded"] is True

        clock[0] += 1.0
        work_again = service.serve(
            "session-work",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="work",
        )
        assert clicked["source_starter_id"] not in {
            topic["source_starter_id"] for topic in work_again["topics"]
        }

        # Design never serves packaged presets, so a work click cannot leak there.
        design = service.serve(
            "session-design",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="design",
        )
        assert design["available"] is False
        assert design.get("topics") in (None, [])
    finally:
        await service.shutdown()


def test_cold_start_click_records_without_model_receipts(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import ArkBackgroundConfig
    from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
        NewPageOutcomeQueue,
        TopicStarterStoreOutcomeSink,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import GLOBAL_NEW_CHAT_SCOPE

    store = TopicStarterStore(tmp_path / "recommendation.db")
    queue = NewPageOutcomeQueue()
    queue.attach_sink(TopicStarterStoreOutcomeSink(store))
    service = TopicService(
        store,
        generate=None,
        search_provider_id="test",
        config_loader=lambda: ArkBackgroundConfig(
            api_key="test", model="test", context_window_tokens=131072,
        ),
        enabled=True,
        now=lambda: 1_700_000_000.0,
    )
    response = service.serve(
        "session-new",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
    )
    assert response["available"] is True
    topic = response["topics"][0]
    assert service.record_exposure(
        {
            "session_id": "session-new",
            "slate_id": response["slate_id"],
            "manifest_id": response["manifest_id"],
            "generation_batch_id": response["generation_batch_id"],
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "exposure_slice_id": response["exposure_slice_id"],
            "topics": [
                {"card_revision_id": item["card_revision_id"]}
                for item in response["topics"]
            ],
        },
        created_at=1_700_000_001.0,
    )
    service.schedule_successor = lambda **_kwargs: None
    recorded = service.record_interaction(
        {
            "session_id": "session-new",
            "slate_id": response["slate_id"],
            "event_type": "topic_click",
            "topic_id": topic["card_revision_id"],
            "payload": {
                "manifest_id": response["manifest_id"],
                "generation_batch_id": response["generation_batch_id"],
                "context_scope": GLOBAL_NEW_CHAT_SCOPE,
                "project_id": None,
                "card_revision_id": topic["card_revision_id"],
            },
        },
        outcome_queue=queue,
        created_at=1_700_000_002.0,
    )
    assert recorded == {"recorded": True}


def test_global_exposure_keeps_corpus_cards_without_an_interest_id():
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        _select_regular_exposure,
    )

    card = {
        "card_id": "cold-corpus:life-001",
        "card_revision_id": "revision-1",
        "source_starter_id": "life-001",
        "interest_summary_id": None,
        "selector_rank": 1,
        "cold_start": True,
    }
    selected = _select_regular_exposure(
        [card],
        eligible_revision_ids={"revision-1"},
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        window_index=0,
        now=1_700_000_000.0,
        shown_before=frozenset(),
    )
    assert [item["card_revision_id"] for item in selected] == ["revision-1"]


def test_personalized_exposure_keeps_interest_cards_without_presets():
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        _select_exposure,
    )

    # Serve passes interest (+ same-pool hot search), never packaged presets.
    cards = [
        {
            "card_id": "interest-1",
            "card_revision_id": "revision-1",
            "interest_summary_id": "interest-a",
            "selector_rank": 1,
            "direction": "a",
        },
    ]
    selected = _select_exposure(
        cards,
        eligible_revision_ids={card["card_revision_id"] for card in cards},
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        window_index=0,
        now=1_700_000_000.0,
    )
    assert [item["card_revision_id"] for item in selected] == ["revision-1"]


def test_generated_serve_counts_news_and_progress_without_interest():
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        HOT_LIST_SOURCE_KIND,
        card_counts_as_generated_serve,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_retrieval import (
        NEWS_SOURCE_KIND,
    )

    # Presets only when interest / hot search / progress / other generated cards
    # are all gone — a lone news or progress card still occupies the slate.
    news_card = {
        "card_id": "news-1",
        "interest_summary_id": None,
        "evidence_bundle": {"source_kind": NEWS_SOURCE_KIND},
    }
    hot_card = {
        "card_id": "hot-1",
        "interest_summary_id": None,
        "evidence_bundle": {"source_kind": HOT_LIST_SOURCE_KIND},
    }
    progress_card = {
        "card_id": "progress-1",
        "category": "profile_recent_progress",
        "interest_summary_id": None,
    }
    preset_card = {
        "card_id": "cold-corpus:life-001",
        "cold_start": True,
        "interest_summary_id": None,
    }
    assert card_counts_as_generated_serve(news_card) is True
    assert card_counts_as_generated_serve(hot_card) is True
    assert card_counts_as_generated_serve(progress_card) is True
    assert card_counts_as_generated_serve(preset_card) is False
    assert card_counts_as_generated_serve(
        {"card_id": "interest-1", "interest_summary_id": "interest-a"}
    ) is True


def test_global_refill_keeps_corpus_cards_without_an_interest_id():
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        _select_exposure,
    )

    cards = [
        {
            "card_id": f"cold-corpus:life-00{index}",
            "card_revision_id": f"revision-{index}",
            "source_starter_id": f"life-00{index}",
            "interest_summary_id": None,
            "selector_rank": index,
            "cold_start": True,
        }
        for index in (1, 2, 3)
    ]
    selected = _select_exposure(
        cards,
        eligible_revision_ids={card["card_revision_id"] for card in cards},
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        window_index=0,
        now=1_700_000_000.0,
        shown_before=frozenset(),
        retained_card_ids=tuple(card["card_id"] for card in cards),
    )
    # A dismissal refills from the previous slate; the preset cards it kept
    # on screen must survive that read, not only the first one.
    assert [item["card_revision_id"] for item in selected] == [
        "revision-1", "revision-2", "revision-3",
    ]


@pytest.mark.asyncio
async def test_gateway_shutdown_skips_unavailable_product_lane_and_awaits_prepare_lane() -> None:
    calls: list[str] = []

    class PrepareLane:
        async def shutdown(self) -> None:
            calls.append("prepare")

    await recommendation_host._shutdown_topic_starter_services(PrepareLane())

    assert calls == ["prepare"]


def test_a_malformed_global_interest_candidate_is_dropped_alone() -> None:
    """A candidate of the wrong shape is dropped without grounding the ones that stand."""

    from types import SimpleNamespace

    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        TopicPipeline,
    )

    valid = {
        "slot_id": "slot-1",
        "interest_summary_id": "interest-1",
        "positions_left": 1,
        "direction": "compare the two options",
        "blocked_by": "none",
        "new_angle": "cost",
        "decision": "write",
        "intent": "compare the options",
        "title": "Compare the two options",
    }
    plans = TopicPipeline._validate_global_interest_candidates(
        {"candidates": [valid, {**valid, "decision": "unsure"}, "not-a-candidate"]},
        snapshot=SimpleNamespace(
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            input_snapshot_id="snapshot-1",
        ),
        ranked=[SimpleNamespace(interest_summary_id="interest-1")],
        summaries_by_id={"interest-1": "compare the two options"},
        source_profile_units_by_interest={"interest-1": ("unit-1",)},
        candidate_slots=[
            {"slot_id": "slot-1", "interest_summary_id": "interest-1"},
        ],
        now=1_700_000_000.0,
    )

    assert [plan["candidate_id"] for plan in plans] == ["slot-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", ["DeepSeek-V4.1-Flash", "doubao-seed"])
async def test_recommendation_request_disables_thinking_for_every_model(
    model_name: str,
) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
        build_ark_stage_invoker,
    )

    class _Message:
        content = "{}"
        refusal = None

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Response:
        id = "provider-response"
        object = "chat.completion"
        model = model_name
        usage = None
        choices = [_Choice()]

    captured: list[dict] = []

    class _Completions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _Response()

    class _Client:
        class chat:  # noqa: N801 - mimics the OpenAI client shape
            completions = _Completions()

        async def close(self) -> None:
            return None

    invoke = build_ark_stage_invoker(
        ArkBackgroundConfig(api_key="test", model=model_name, expected_model=model_name),
        client_factory=lambda **_kwargs: _Client(),
    )
    await invoke("v6_utility_scorer", "system", "{}")

    assert captured[0]["extra_body"] == {
        "thinking": {"type": "disabled"},
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert "reasoning_effort" not in captured[0]
    # No stage budget hook → invoker default floor (never omit / never 4k provider default).
    assert captured[0]["max_tokens"] == 16384


def test_v6_max_tokens_matches_designer_structured_budgets() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _v6_max_tokens,
    )

    assert _v6_max_tokens("v6_session_summarizer") == 65536
    assert _v6_max_tokens("v6_utility_scorer") == 32768
    assert _v6_max_tokens("v6_hot_news_matcher") == 16384


def test_clamp_topic_max_tokens_respects_context_window() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        _clamp_topic_max_tokens,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _output_budget_for_stage,
        _v6_max_tokens,
    )

    # Half-window room keeps prompt fit from collapsing on small windows.
    assert _clamp_topic_max_tokens(32768, context_window_tokens=8192) == 4096
    assert _clamp_topic_max_tokens(16384, context_window_tokens=131072) == 16384
    assert _output_budget_for_stage("rid_interest_distiller", 32768) == 16384
    assert _output_budget_for_stage(
        "v6_session_summarizer", 262144,
    ) == _v6_max_tokens("v6_session_summarizer")


def test_load_all_session_records_caps_to_newest_fifty(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    history.clear_session_window_cache()
    requested: dict[str, int] = {}
    metadata = [
        {"session_id": f"s-{index}"}
        for index in range(history._ALL_SESSIONS_LIMIT + 12)
    ]

    def fake_metadata(*, limit: int, offset: int):
        requested["limit"] = limit
        requested["offset"] = offset
        return metadata[offset: offset + limit], len(metadata)

    def fake_load(session_id: str):
        return [{"id": session_id}]

    monkeypatch.setattr(history, "get_all_sessions_metadata", fake_metadata)
    monkeypatch.setattr(history, "load_session_records", fake_load)

    loaded = history.load_all_session_records()
    assert requested == {"limit": history._ALL_SESSIONS_LIMIT, "offset": 0}
    assert history._ALL_SESSIONS_LIMIT == 50
    assert list(loaded) == [f"s-{index}" for index in range(50)]
    assert history.load_all_session_records(limit=5) == {
        f"s-{index}": [{"id": f"s-{index}"}] for index in range(5)
    }


def test_recommendation_history_projection_keeps_ordinals_and_boundaries(
    monkeypatch, tmp_path,
) -> None:
    """The scan drops transcript-only payloads without shifting physical ordinals."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    rows = [
        {
            "id": "r1:user",
            "role": "user",
            "request_id": "r1",
            "timestamp": 1.0,
            "content": "u" * 40_000,
            "input_origin": "manual_free_text",
            "assistance_origin": "none",
            "metadata": {"automation": {"kind": "heartbeat"}},
            "prompt_suggestion": {"id": "p1"},
        },
        {
            "id": "r1:llm",
            "role": "assistant",
            "request_id": "r1",
            "timestamp": 1.1,
            "event_type": "chat.llm_call_start",
            "content": "",
            "prompt": "x" * 1_000,
            "metadata": {"usage": "y" * 1_000},
        },
        {
            "id": "r1:activity",
            "role": "assistant",
            "request_id": "r1",
            "timestamp": 1.15,
            "event_type": "chat.subagent_activity",
            "content": "progress noise",
        },
        {
            "id": "r1:final",
            "role": "assistant",
            "request_id": "r1",
            "timestamp": 1.18,
            "event_type": "chat.final",
            "content": "x" * 40_000,
        },
        {
            "id": "r1:done",
            "role": "assistant",
            "request_id": "r1",
            "timestamp": 1.2,
            "event_type": "chat.request_completed",
            "status": "success",
            "feedback_only": True,
        },
    ]
    path = tmp_path / "history.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "get_read_history_path", lambda _sid: path)

    projected = history.load_session_records("s-1")

    assert len(projected) == len(rows)
    assert len(projected[0]["content"]) == history._MAX_RETAINED_CONTENT_CHARS
    assert projected[0]["metadata"] == {"automation": {"kind": "heartbeat"}}
    assert "prompt" not in projected[1]
    assert "metadata" not in projected[1]
    assert "content" not in projected[2]
    assert len(projected[3]["content"]) == history._MAX_RETAINED_CONTENT_CHARS
    assert projected[4]["feedback_only"] is True
    assert projected[4]["status"] == "success"


def test_recommendation_history_stream_keeps_unicode_separators(
    monkeypatch, tmp_path,
) -> None:
    """A U+2028 inside a record is content, not a line break."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    path = tmp_path / "history.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "u:0",
                "role": "user",
                "request_id": "u",
                "timestamp": 1.0,
                "content": "before after",
                "input_origin": "manual_free_text",
                "assistance_origin": "none",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "get_read_history_path", lambda _sid: path)

    records = history.load_session_records("s-unicode")

    assert len(records) == 1
    assert records[0]["content"] == "before after"


def test_rescan_accepts_a_clipped_witness_for_a_long_user_turn() -> None:
    """Old ledgers store the full text; clipping both sides avoids a false rewrite."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    full = "u" * 40_000
    record = {
        "id": "u:0",
        "role": "user",
        "request_id": "u",
        "timestamp": 1.0,
        "content": history.witness_content(full),
        "input_origin": "manual_free_text",
        "assistance_origin": "none",
    }
    ledger = {
        "history_revision": 1,
        "next_behavior_index": 2,
        "sessions": {
            "s": {
                "entries": [{
                    "ordinal": 0,
                    "behavior_index": 1,
                    "retracted": False,
                    "witness": {
                        "role": "user",
                        "timestamp_or_0": 1.0,
                        "content": full,
                        "supported": True,
                    },
                }],
                "registered_upto": [],
            },
        },
    }

    rescan = history.rescan_history(ledger, {"s": [record]}, {})

    assert rescan.rewrite_detected is False
    assert rescan.retracted_indices == []
    persisted = rescan.ledger["sessions"]["s"]["entries"][0]["witness"]["content"]
    assert persisted.startswith("witness-sha256:v1:")
    assert full not in persisted


def test_record_witness_stores_a_digest_not_the_text() -> None:
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    full = "secret user text " * 100
    witness = history.record_witness(
        {"role": "user", "content": full, "timestamp": 1.0}, True,
    )

    assert full not in witness["content"]
    assert witness["content"].startswith("witness-sha256:v1:")

    legacy = {
        "role": "user", "timestamp_or_0": 1.0, "content": full, "supported": True,
    }
    assert history.normalize_witness(legacy)["content"] == witness["content"]
    assert history.normalize_witness(witness) == witness


def test_history_scan_budget_keeps_newest_sessions(monkeypatch, tmp_path) -> None:
    """Newest-first: the first session is read, later ones stop at the budget."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    history.clear_session_window_cache()
    sizes = {"newest": 60, "middle": 60, "oldest": 60}
    monkeypatch.setattr(
        history,
        "get_all_sessions_metadata",
        lambda **_kwargs: (
            [{"session_id": sid} for sid in sizes],
            len(sizes),
        ),
    )
    monkeypatch.setattr(history, "_MAX_SCAN_BYTES", 100)

    def fake_path(session_id):
        path = tmp_path / f"{session_id}.jsonl"
        path.write_text("x" * sizes[session_id], encoding="utf-8")
        return path

    monkeypatch.setattr(history, "get_read_history_path", fake_path)
    read: list[str] = []

    def fake_load(session_id):
        read.append(session_id)
        return []

    monkeypatch.setattr(history, "load_session_records", fake_load)

    result = history.load_all_session_records()

    assert read == ["newest"]
    assert result == {"newest": []}


def test_run_snapshot_serves_one_history_across_the_run(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    reads: list[str] = []
    monkeypatch.setattr(
        history, "load_session_records", lambda sid: reads.append(sid) or [],
    )
    token = history.set_run_records_snapshot({"s1": [{"id": "s1"}]})
    try:
        first = history.load_all_session_records()
        second = history.load_all_session_records()
        first["s1"].append({"id": "mutated"})
        third = history.load_all_session_records()

        assert reads == []
        assert second == {"s1": [{"id": "s1"}]}
        assert third == {"s1": [{"id": "s1"}]}
    finally:
        history.reset_run_records_snapshot(token)


def test_session_records_are_read_once_per_burst(monkeypatch, tmp_path) -> None:
    """Revision, summaries and short intent in one run share one history read."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    reads: list[str] = []
    path = tmp_path / "s1.jsonl"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        history, "get_all_sessions_metadata",
        lambda **_kwargs: ([{"session_id": "s1"}], 1),
    )
    monkeypatch.setattr(history, "get_read_history_path", lambda _sid: path)

    def fake_load(session_id):
        reads.append(session_id)
        return [{"id": session_id}]

    monkeypatch.setattr(history, "load_session_records", fake_load)
    history.clear_session_window_cache()
    try:
        first = history.load_all_session_records()
        second = history.load_all_session_records()
        first["s1"].append({"id": "mutated"})
        third = history.load_all_session_records()

        assert reads == ["s1"]
        assert [row["id"] for row in second["s1"]] == ["s1"]
        assert [row["id"] for row in third["s1"]] == ["s1"]
    finally:
        history.clear_session_window_cache()


def test_clear_during_a_scan_does_not_repopulate_the_cache(monkeypatch, tmp_path) -> None:
    """A clear that lands mid-scan must not be undone by the scan's late write."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    reads: list[str] = []
    path = tmp_path / "s1.jsonl"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        history, "get_all_sessions_metadata",
        lambda **_kwargs: ([{"session_id": "s1"}], 1),
    )
    monkeypatch.setattr(history, "get_read_history_path", lambda _sid: path)

    def fake_load(session_id):
        reads.append(session_id)
        if len(reads) == 1:
            history.clear_session_window_cache()
        return [{"id": session_id}]

    monkeypatch.setattr(history, "load_session_records", fake_load)
    history.clear_session_window_cache()
    try:
        first = history.load_all_session_records()
        second = history.load_all_session_records()

        assert reads == ["s1", "s1"]
        assert first == second == {"s1": [{"id": "s1"}]}
    finally:
        history.clear_session_window_cache()


def test_clear_does_not_wait_for_an_in_flight_scan(monkeypatch, tmp_path) -> None:
    """A completion's clear runs on the event loop; it must not block on the scan lock."""

    import threading
    import time

    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    started = threading.Event()
    release = threading.Event()
    path = tmp_path / "s1.jsonl"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        history, "get_all_sessions_metadata",
        lambda **_kwargs: ([{"session_id": "s1"}], 1),
    )
    monkeypatch.setattr(history, "get_read_history_path", lambda _sid: path)

    def slow_load(_session_id):
        started.set()
        release.wait(0.75)
        return [{"id": "s1"}]

    monkeypatch.setattr(history, "load_session_records", slow_load)
    history.clear_session_window_cache()
    scan = threading.Thread(target=history.load_all_session_records, daemon=True)
    try:
        scan.start()
        assert started.wait(1.0)
        began = time.monotonic()
        history.clear_session_window_cache()
        elapsed = time.monotonic() - began
        assert elapsed < 0.5
    finally:
        release.set()
        scan.join(timeout=2.0)
        history.clear_session_window_cache()


@pytest.mark.asyncio
async def test_a_queued_run_is_the_candidate_input_signal(tmp_path) -> None:
    """The event loop must not compute a revision to notice a queued request."""

    from types import SimpleNamespace

    service = _topic_service(tmp_path)
    calls: list[tuple] = []

    def fail(*args):
        calls.append(args)
        raise AssertionError("revision must not be computed on the event loop")

    service._input_revision_loader = fail
    try:
        slot = SimpleNamespace(queued=object(), receipt={"revision_digest": "x"})
        assert service._candidate_input_changed("global_new_chat", slot) is True
        assert calls == []
    finally:
        await service.shutdown()


def test_newest_session_window_is_shared_within_ttl(monkeypatch, tmp_path) -> None:
    """One burst of scans (enable seed, concurrent scopes) enumerates once."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    calls: list[dict] = []

    def fake_metadata(**kwargs):
        calls.append(kwargs)
        return [{"session_id": "s1"}], 1

    monkeypatch.setattr(history, "get_all_sessions_metadata", fake_metadata)
    monkeypatch.setattr(history, "load_session_records", lambda _sid: [])
    monkeypatch.setattr(history, "get_read_history_path", lambda _sid: tmp_path / "none.jsonl")
    history.clear_session_window_cache()
    try:
        history.load_all_session_records()
        history.load_all_session_records()
        assert len(calls) == 1

        history.clear_session_window_cache()
        history.load_all_session_records()
        assert len(calls) == 2
    finally:
        history.clear_session_window_cache()


def test_scope_input_revision_reads_metadata_for_loaded_sessions_only(
    monkeypatch, tmp_path,
) -> None:
    from jiuwenswarm.agents.harness.common.recommendation import topic_pipeline as pipeline_module
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import TopicPipeline
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
        TopicStarterStore,
    )

    pipeline = TopicPipeline(TopicStarterStore(tmp_path / "revision-metadata.db"))
    records = {
        "s-alpha": [{
            "id": "s-alpha:0",
            "role": "user",
            "request_id": "r-alpha",
            "timestamp": 10.0,
            "content": "alpha thought",
            "input_origin": "manual_free_text",
            "assistance_origin": "none",
        }],
        "s-beta": [{
            "id": "s-beta:0",
            "role": "user",
            "request_id": "r-beta",
            "timestamp": 11.0,
            "content": "beta thought",
            "input_origin": "manual_free_text",
            "assistance_origin": "none",
        }],
    }
    pipeline.completed_records_loader = lambda: records
    requested: list[str] = []

    def fake_metadata(session_id, **_kwargs):
        requested.append(session_id)
        return {"project_id": "", "work_mode": "work"}

    monkeypatch.setattr(pipeline_module, "get_session_metadata", fake_metadata)

    pipeline.scope_input_revision(GLOBAL_NEW_CHAT_SCOPE, "work")

    assert sorted(requested) == ["s-alpha", "s-beta"]


@pytest.mark.asyncio
async def test_refresh_short_intent_reads_metadata_for_loaded_sessions_only(
    monkeypatch, tmp_path,
) -> None:
    from jiuwenswarm.agents.harness.common.recommendation import topic_pipeline as pipeline_module
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import TopicPipeline
    from jiuwenswarm.agents.harness.common.recommendation.topic_prepare import (
        V5GenerationInputs,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
        TopicStarterStore,
    )

    pipeline = TopicPipeline(
        TopicStarterStore(tmp_path / "short-intent-metadata.db"),
        now=lambda: 100.0,
    )
    records = {
        "s-alpha": [{
            "id": "s-alpha:0",
            "role": "user",
            "request_id": "r-alpha",
            "timestamp": 10.0,
            "content": "alpha thought",
            "input_origin": "manual_free_text",
            "assistance_origin": "none",
        }],
    }
    pipeline.completed_records_loader = lambda: records
    inputs = V5GenerationInputs(
        state={},
        active_behaviors=(),
        delta_behaviors=(),
        generation_input_snapshot={},
    )
    requested: list[str] = []

    def fake_metadata(session_id, **_kwargs):
        requested.append(session_id)
        return {"project_id": ""}

    monkeypatch.setattr(pipeline_module, "load_generation_inputs", lambda *_a, **_k: inputs)
    monkeypatch.setattr(pipeline_module, "get_session_metadata", fake_metadata)

    assert await pipeline.refresh_short_intent(GLOBAL_NEW_CHAT_SCOPE) == ""

    assert requested == ["s-alpha"]


def test_drop_expired_pool_cards_renumbers_live_ranks() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_pool import (
        drop_expired_pool_cards,
    )

    now = 1_700_000_000.0
    cards = [
        {
            "card_revision_id": "alive",
            "selector_rank": 1,
            "expire_time": now + 60,
        },
        {
            "card_revision_id": "dead",
            "selector_rank": 2,
            "expire_time": now - 1,
        },
        {
            "card_revision_id": "also-alive",
            "selector_rank": 3,
            "expire_time": now + 120,
        },
    ]
    manifest = {"card_revision_ids": ["alive", "dead", "also-alive"], "other": 1}
    live, updated = drop_expired_pool_cards(cards, manifest, now=now)
    assert [card["card_revision_id"] for card in live] == ["alive", "also-alive"]
    assert [card["selector_rank"] for card in live] == [1, 2]
    assert updated["card_revision_ids"] == ["alive", "also-alive"]
    assert updated["other"] == 1


def test_topic_minimal_reasoning_prefers_off(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.recommendation import topic_model as tm

    class _Capability:
        options = ("off", "low", "medium", "high")

    monkeypatch.setattr(
        "openjiuwen.core.foundation.llm.get_reasoning_capability",
        lambda **_kwargs: _Capability(),
    )
    config = tm.ArkBackgroundConfig(
        api_key="k",
        model="glm-4-flash",
        base_url="https://example.com/v1",
    )
    assert tm._minimal_reasoning(config) == {"mode": "disabled"}


@pytest.mark.asyncio
async def test_recommendation_model_path_passes_minimal_reasoning(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
        build_ark_stage_invoker,
    )

    captured: dict[str, Any] = {}

    class _Assistant:
        content = "{}"
        refusal = None
        response_id = "resp-1"
        response_model = "glm-4-flash"
        finish_reason = "stop"
        usage_metadata = None

    class _Model:
        async def invoke(self, messages, **kwargs):
            captured["kwargs"] = kwargs
            return _Assistant()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.recommendation.topic_model._minimal_reasoning",
        lambda _config: {"mode": "disabled"},
    )
    invoke = build_ark_stage_invoker(
        ArkBackgroundConfig(api_key="k", model="glm-4-flash", expected_model="glm-4-flash"),
        model_factory=lambda _config: _Model(),
        max_tokens_for_stage=lambda _stage: 32768,
        require_response_metadata=False,
    )
    await invoke("v6_utility_scorer", "system", "{}")
    assert captured["kwargs"]["max_tokens"] == 32768
    assert captured["kwargs"]["reasoning"] == {"mode": "disabled"}
    assert captured["kwargs"]["extra_body"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_recommendation_model_path_drops_reasoning_on_http_400(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
        build_ark_stage_invoker,
    )

    calls: list[dict[str, Any]] = []

    class _HttpError(Exception):
        status_code = 400

    class _Assistant:
        content = "{}"
        refusal = None
        response_id = "resp-retry"
        response_model = "glm-4-flash"
        finish_reason = "stop"
        usage_metadata = None

    class _Model:
        async def invoke(self, messages, **kwargs):
            calls.append(dict(kwargs))
            if "reasoning" in kwargs:
                raise _HttpError("endpoint rejected reasoning")
            return _Assistant()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.recommendation.topic_model._minimal_reasoning",
        lambda _config: {"mode": "disabled"},
    )
    receipts: list[dict[str, Any]] = []
    invoke = build_ark_stage_invoker(
        ArkBackgroundConfig(api_key="k", model="glm-4-flash", expected_model="glm-4-flash"),
        model_factory=lambda _config: _Model(),
        max_tokens_for_stage=lambda _stage: 4096,
        require_response_metadata=False,
        receipt_sink=receipts.append,
    )
    result = await invoke("v6_utility_scorer", "system", "{}")
    assert result == {}
    assert len(calls) == 2
    assert "reasoning" in calls[0]
    assert "reasoning" not in calls[1]
    assert any(item.get("reasoning_dropped") is True for item in receipts)


@pytest.mark.asyncio
async def test_v6_ark_stage_invoker_caps_parallel_provider_calls() -> None:
    """Utility/diversity used to fan out past provider max_parallel_requests."""
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _TOPIC_ARK_STAGE_CONCURRENCY,
        build_v6_ark_stage_invoker,
    )

    class _Message:
        content = "{}"
        refusal = None

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Response:
        id = "provider-response"
        object = "chat.completion"
        model = "Qwen3.8-Flash"
        usage = None
        choices = [_Choice()]

    peak = 0
    inflight = 0
    lock = asyncio.Lock()

    class _Completions:
        async def create(self, **_kwargs):
            nonlocal peak, inflight
            async with lock:
                inflight += 1
                peak = max(peak, inflight)
            await asyncio.sleep(0.05)
            async with lock:
                inflight -= 1
            return _Response()

    class _Client:
        class chat:  # noqa: N801 - mimics the OpenAI client shape
            completions = _Completions()

        async def close(self) -> None:
            return None

    limit = 2
    invoke = build_v6_ark_stage_invoker(
        ArkBackgroundConfig(
            api_key="test", model="Qwen3.8-Flash", expected_model="Qwen3.8-Flash",
        ),
        client_factory=lambda **_kwargs: _Client(),
        max_concurrency=limit,
    )
    assert invoke.max_concurrency == limit
    assert _TOPIC_ARK_STAGE_CONCURRENCY == 3

    await asyncio.gather(*(
        invoke("v6_utility_scorer", "system", '{"request_id":"r","candidates":[]}')
        for _ in range(6)
    ))
    assert peak == limit
    assert inflight == 0


def test_candidate_interest_slots_keep_only_the_best_ranked() -> None:
    """The tail beyond the best-ranked interests only costs a pairwise score."""

    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _MAX_INTEREST_CANDIDATES,
        _top_ranked_interests,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_retrieval import (
        RankedStarterInterest,
    )

    interests = [
        RankedStarterInterest(
            interest_summary_id=f"interest-{rank}",
            interest_summary=f"summary {rank}",
            rank=rank,
        )
        for rank in (3, 1, 5, 2, 4, 7, 6, 9, 8)
    ]

    kept = _top_ranked_interests(interests)

    assert len(kept) == _MAX_INTEREST_CANDIDATES
    # The highest ranks survive and the released order is preserved.
    assert [item.rank for item in kept] == [3, 1, 5, 2, 4, 6]
    assert _top_ranked_interests(interests[:3]) == interests[:3]


@pytest.mark.asyncio
async def test_diversity_batches_carry_only_the_candidates_they_score() -> None:
    """A pair batch re-sent the whole candidate list; only its pairs decide it."""

    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _invoke_batched_diversity_proxy,
    )

    candidates = [
        {"candidate_id": f"candidate-{index}", "title": f"title {index}"}
        for index in range(12)
    ]
    sent: list[dict[str, Any]] = []

    async def invoke_stage(_stage: str, _prompt: str, payload: str) -> Any:
        request = json.loads(payload)
        sent.append(request)
        return {
            "pairwise_scores": [
                {
                    "left_candidate_id": pair["left_candidate_id"],
                    "right_candidate_id": pair["right_candidate_id"],
                    "reason": "different explanations",
                    "compatible": True,
                    "diversity_score_nullable": 0.5,
                }
                for pair in request["requested_pairs"]
            ],
            "marginal_scores": [
                {
                    "candidate_id": candidate_id,
                    "marginal_diversity_nullable": 0.5,
                }
                for candidate_id in request["requested_marginal_candidate_ids"]
            ],
        }

    result = await _invoke_batched_diversity_proxy(
        {"input_snapshot_id": "snapshot", "request_id": "r", "candidates": candidates},
        invoke_stage=invoke_stage,
    )

    assert len(sent) > 1
    assert len(result["pairwise_scores"]) == len(candidates) * (len(candidates) - 1) // 2
    for request in sent:
        involved = set(request["requested_marginal_candidate_ids"])
        for pair in request["requested_pairs"]:
            involved.add(pair["left_candidate_id"])
            involved.add(pair["right_candidate_id"])
        assert {item["candidate_id"] for item in request["candidates"]} == involved
    # A pair-only batch leaves the candidates it never asked about out entirely.
    assert any(len(request["candidates"]) < len(candidates) for request in sent)


def test_outcome_queue_publish_and_attach_do_not_lose_thread_signals() -> None:
    """The RPC publishes from a worker thread while the sink may still attach."""

    from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
        NewPageOutcomeQueue,
        NewPageOutcomeSignal,
    )

    def signal(index: int) -> NewPageOutcomeSignal:
        return NewPageOutcomeSignal(
            outcome="accepted",
            manifest_id=f"manifest-{index}",
            generation_batch_id="batch-1",
            exposure_id=f"exposure-{index}",
            card_revision_id=f"revision-{index}",
            context_scope="global_new_chat",
            project_id=None,
            component_request_ids=(),
            provider_response_ids=(),
            search_receipt=None,
            occurred_at=1.0,
            host_written=True,
        )

    queue = NewPageOutcomeQueue()
    delivered: list[NewPageOutcomeSignal] = []

    def sink(item: NewPageOutcomeSignal) -> bool:
        delivered.append(item)
        return True

    items = [signal(index) for index in range(40)]
    publishers = [
        threading.Thread(target=queue.publish, args=(item,)) for item in items
    ]
    for publisher in publishers:
        publisher.start()
    assert queue.attach_sink(sink) is not None
    for publisher in publishers:
        publisher.join()

    assert queue.pending_count == 0
    assert sorted(item.exposure_id for item in delivered) == sorted(
        item.exposure_id for item in items
    )


def test_global_pool_target_leaves_five_ordinary_slots_beside_the_news_card() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_selection import (
        GLOBAL_POOL_TARGET_SIZE,
        ListSearchConstraints,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
    )

    assert GLOBAL_POOL_TARGET_SIZE == 6
    assert ListSearchConstraints(
        context_scope=GLOBAL_NEW_CHAT_SCOPE, leading_cards=1,
    ).target_size == 5
    assert ListSearchConstraints(
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
    ).target_size == 6


@pytest.mark.asyncio
async def test_recommendation_retries_when_model_rejects_disabled_thinking() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
        build_ark_stage_invoker,
    )

    class _Message:
        content = '{"ok":true}'
        refusal = None

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Response:
        id = "provider-response"
        object = "chat.completion"
        model = "GLM-5.3-Flash"
        usage = None
        choices = [_Choice()]

    captured: list[dict] = []

    class _Completions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            if kwargs.get("extra_body", {}).get("thinking", {}).get("type") == "disabled":
                raise RuntimeError(
                    "Error code: 400 - {'error': {'message': "
                    "'该模型始终思考，不支持关闭思考；请使用 low、high 或 max。'}}"
                )
            return _Response()

    class _Client:
        class chat:  # noqa: N801 - mimics the OpenAI client shape
            completions = _Completions()

        async def close(self) -> None:
            return None

    invoke = build_ark_stage_invoker(
        ArkBackgroundConfig(
            api_key="test",
            model="GLM-5.3-Flash",
            expected_model="GLM-5.3-Flash",
        ),
        client_factory=lambda **_kwargs: _Client(),
    )
    assert await invoke("v6_utility_scorer", "system", "{}") == {"ok": True}
    assert len(captured) == 2
    assert captured[0]["extra_body"] == {
        "thinking": {"type": "disabled"},
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert captured[1]["extra_body"] == {}


@pytest.mark.asyncio
async def test_recommendation_stage_invoker_uses_core_model_factory() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
        build_ark_stage_invoker,
    )

    class _Assistant:
        content = '{"ok":true}'
        refusal = None
        response_id = "core-response-1"
        response_model = "core-model"
        finish_reason = "stop"
        usage_metadata = {"prompt_tokens": 1, "completion_tokens": 1}

    captured: list[dict] = []

    class _Model:
        async def invoke(self, messages, **kwargs):
            captured.append({"messages": messages, **kwargs})
            return _Assistant()

    invoke = build_ark_stage_invoker(
        ArkBackgroundConfig(
            api_key="test",
            model="core-model",
            expected_model="core-model",
            base_url="https://example.test/v1",
        ),
        model_factory=lambda _config: _Model(),
        require_response_metadata=True,
        return_response_metadata=True,
    )
    payload = await invoke("v6_utility_scorer", "system", "{}")

    assert payload["result"] == {"ok": True}
    assert payload["provider_response_id"] == "core-response-1"
    assert payload["actual_model"] == "core-model"
    assert captured[0]["extra_body"] == {
        "thinking": {"type": "disabled"},
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert "response_format" not in captured[0]


@pytest.mark.asyncio
async def test_recommendation_stage_invoker_parses_fenced_json_without_response_format() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
        build_ark_stage_invoker,
    )

    class _Message:
        content = 'Here you go:\n```json\n{"ok": true, "items": [1]}\n```\n'
        refusal = None

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Response:
        id = "provider-response"
        object = "chat.completion"
        model = "DeepSeek-V4.1-Flash"
        usage = None
        choices = [_Choice()]

    captured: list[dict] = []
    receipts: list[dict] = []

    class _Completions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _Response()

    class _Client:
        class chat:  # noqa: N801 - mimics the OpenAI client shape
            completions = _Completions()

        async def close(self) -> None:
            return None

    invoke = build_ark_stage_invoker(
        ArkBackgroundConfig(
            api_key="test",
            model="DeepSeek-V4.1-Flash",
            expected_model="DeepSeek-V4.1-Flash",
        ),
        client_factory=lambda **_kwargs: _Client(),
        receipt_sink=receipts.append,
    )
    assert await invoke("v6_utility_scorer", "system", "{}") == {
        "ok": True,
        "items": [1],
    }
    assert "response_format" not in captured[0]
    assert any(
        receipt.get("structured_output_mode") == "prompt_json"
        and receipt.get("status") == "parsed"
        for receipt in receipts
    )


def test_memory_response_schema_omits_array_keywords_dashscope_rejects() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _v6_response_schema,
    )

    forbidden = {"uniqueItems", "contains", "minContains", "maxContains"}

    def keywords(node: object) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            found.update(forbidden & set(node))
            for value in node.values():
                found.update(keywords(value))
        elif isinstance(node, list):
            for value in node:
                found.update(keywords(value))
        return found

    for stage in ("memory_init", "memory_incremental"):
        assert keywords(_v6_response_schema(stage)) == set()


@pytest.mark.asyncio
async def test_unconsumed_validation_receipts_stay_bounded() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        _MAX_PENDING_VALIDATION_RECEIPTS,
        ArkBackgroundConfig,
        build_ark_stage_invoker,
    )

    class _Message:
        content = "{}"
        refusal = None

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Response:
        id = "provider-response"
        object = "chat.completion"
        model = "test"
        usage = None
        choices = [_Choice()]

    class _Completions:
        async def create(self, **_kwargs):
            return _Response()

    class _Client:
        class chat:  # noqa: N801 - mimics the OpenAI client shape
            completions = _Completions()

        async def close(self) -> None:
            return None

    receipts: list[dict] = []
    invoke = build_ark_stage_invoker(
        ArkBackgroundConfig(api_key="test", model="test"),
        client_factory=lambda **_kwargs: _Client(),
        receipt_sink=receipts.append,
    )
    # A caller that fails before record_validation must not let results accumulate forever.
    results = [
        await invoke("stage", "system", "{}")
        for _ in range(_MAX_PENDING_VALIDATION_RECEIPTS + 20)
    ]
    for result in results:
        invoke.record_validation(result, "ok")

    recorded = [receipt for receipt in receipts if receipt.get("status") == "ok"]
    assert len(recorded) == _MAX_PENDING_VALIDATION_RECEIPTS


def test_recommendation_stores_wait_the_same_for_a_write_lock(tmp_path) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_short_intent import (
        ShortIntentStateStore,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
        TopicStarterStore,
    )

    topic_store = TopicStarterStore(tmp_path / "recommendation.db")
    short_store = ShortIntentStateStore(topic_store)
    for store in (topic_store, short_store):
        with store._connect() as connection:
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


@pytest.mark.asyncio
async def test_saved_topic_sync_logs_when_agent_server_rejects(monkeypatch) -> None:
    from agent_gateway.app_gateway import sync_saved_topic_recommendation

    warnings: list[str] = []

    def record(message: str, *args: Any, **_kwargs: Any) -> None:
        warnings.append(message % args if args else message)

    async def reject(**_kwargs):
        return False, {"error": "AgentServer is unavailable", "code": "SERVICE_UNAVAILABLE"}

    monkeypatch.setattr("agent_gateway.routing.e2a_proxy.fetch_agent_unary", reject)
    monkeypatch.setattr("agent_gateway.app_gateway.logger.warning", record)
    await sync_saved_topic_recommendation(
        object(),
        updated_env_keys=["topic_recommendation_enabled"],
        reload_scopes=[],
        config_payload={},
    )
    assert warnings == [
        "[App] topic recommendation config sync failed: "
        "code=SERVICE_UNAVAILABLE error=AgentServer is unavailable"
    ]


@pytest.mark.asyncio
async def test_recommendation_host_stop_failure_does_not_escape() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    class _Host:
        async def shutdown(self) -> None:
            raise RuntimeError("recommendation stop failed")

    server = AgentWebSocketServer()
    server._recommendation_host = _Host()
    await server._stop_recommendation_host()
    assert server._recommendation_host is None


@pytest.mark.asyncio
async def test_rebind_reports_success_when_recommendation_hook_fails(monkeypatch) -> None:
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_to_agent_response
    from jiuwenswarm.common.e2a.models import E2AResponse
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    class _Project:
        project_id = "proj-new"
        project_dir = "D:/work"
        name = "work"
        work_mode = "code"

    rebound = {"count": 0}

    def metadata(sid, cache_bust=False, enable_writeback=True):
        return {"project_id": "proj-old", "session_id": sid}

    def rebind(**_kwargs):
        rebound["count"] += 1
        return True

    class _Host:
        async def session_left_project(self, project_id: str, session_id: str) -> None:
            raise RuntimeError("sqlite locked")

    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server.get_session_metadata",
        metadata,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.find_or_create_code_project_for_tui_params",
        lambda _params: _Project(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.rebind_session_project",
        rebind,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.recommendation_host.active_recommendation_host",
        lambda: _Host(),
    )

    class _Socket:
        def __init__(self) -> None:
            self.frames: list[dict[str, Any]] = []

        async def send(self, data: str | bytes) -> None:
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            self.frames.append(json.loads(data))

    socket = _Socket()
    await AgentWebSocketServer()._handle_session_rebind_project(
        socket,
        AgentRequest(
            request_id="rebind-1",
            channel_id="web",
            session_id="sess-1",
            req_method=ReqMethod.SESSION_REBIND_PROJECT,
            params={"project_dir": "D:/work"},
        ),
        asyncio.Lock(),
    )
    assert rebound["count"] == 1
    response = e2a_response_to_agent_response(E2AResponse.from_dict(socket.frames[-1]))
    assert response.ok is True
    assert response.payload["project_id"] == "proj-new"


def test_topic_service_has_no_pending_hold_or_manual_retry() -> None:
    """Replenishment uses the durable completed boundary; no inflight hold / retry origin."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_service as ts

    assert "manual_retry" not in ts._ORIGIN_RANK
    assert "chat_request_inflight" not in ts._QUIET_OUTCOMES
    assert not hasattr(ts.TopicService, "_held_for_chat")
    assert not hasattr(ts.TopicService, "_keep_for_chat")
    assert not hasattr(ts.TopicService, "scope_has_pending_request")


def test_dedicated_topic_model_uses_endpoint_mandated_sampling(settings) -> None:
    """Kimi endpoints accept one temperature; the 0.0 background default must not be sent."""

    settings["recommendation"] = {"model_config": {
        "provider": "OpenAI", "model": "kimi-k3.1-pro",
        "api_base": "https://api.moonshot.cn/v1", "api_key": "test-key",
        "context_window_tokens": 262144,
    }}
    config = controls.recommendation_model_config(environ={})
    assert config is not None
    assert (config.effective_temperature, config.effective_top_p) == (1.0, 0.95)


def test_environ_topic_model_uses_endpoint_mandated_sampling() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
    )

    config = ArkBackgroundConfig.from_environ({
        "ARK_API_KEY": "test-key", "ARK_MODEL": "kimi-k3.1-pro",
        "ARK_BASE_URL": "https://api.moonshot.cn/v1",
        "ARK_CONTEXT_WINDOW_TOKENS": "262144",
    })
    assert config is not None
    assert (config.effective_temperature, config.effective_top_p) == (1.0, 0.95)


def test_history_model_copy_uses_endpoint_mandated_sampling() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
        _effective_model_config_obj,
    )

    kimi = _effective_model_config_obj({
        "model_client_config": {
            "api_base": "https://api.moonshot.cn/v1", "model_name": "kimi-k3.1-pro",
        },
        "model_config_obj": {"context_window": 262144},
    })
    assert (kimi["temperature"], kimi["top_p"]) == (1.0, 0.95)
    assert kimi["context_window"] == 262144

    pinned = _effective_model_config_obj({
        "model_client_config": {
            "api_base": "https://api.deepseek.com", "model_name": "deepseek-chat",
        },
        "model_config_obj": {},
    })
    assert pinned["temperature"] == 0.0
    assert "top_p" not in pinned


@pytest.mark.asyncio
async def test_stage_invoker_repairs_provider_required_temperature() -> None:
    """A 400 naming the only allowed temperature is retried once with that value."""

    from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
        ArkBackgroundConfig,
        build_ark_stage_invoker,
    )

    class _BadRequestError(Exception):
        status_code = 400

    class _Message:
        content = "{}"
        refusal = None

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Response:
        id = "provider-response"
        object = "chat.completion"
        model = "kimi-k3.1-pro"
        usage = None
        choices = [_Choice()]

    captured: list[dict] = []

    class _Completions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            if len(captured) == 1:
                raise _BadRequestError(
                    "Error code: 400 - {'error': {'message': "
                    "'invalid temperature: only 0.6 is allowed for this model'}}"
                )
            return _Response()

    class _Client:
        class chat:  # noqa: N801 - mimics the OpenAI client shape
            completions = _Completions()

        async def close(self) -> None:
            return None

    invoke = build_ark_stage_invoker(
        ArkBackgroundConfig(
            api_key="test", model="kimi-k3.1-pro", expected_model="kimi-k3.1-pro",
        ),
        client_factory=lambda **_kwargs: _Client(),
    )

    assert await invoke("v6_utility_scorer", "system", "{}") == {}
    assert [call["temperature"] for call in captured] == [0.0, 0.6]

def test_clicked_card_context_does_not_read_source_history(tmp_path, monkeypatch):
    """The click path must use the card's own evidence, not re-read source sessions."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_controls, topic_product
    from jiuwenswarm.server.runtime.agent_adapter import interface

    (tmp_path / "recommendation.db").touch()
    monkeypatch.setattr(interface, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        interface,
        "_history_user_extra",
        lambda _params: {"topic_starter": {"manifest_id": "manifest-1"}},
    )
    monkeypatch.setattr(
        topic_product,
        "find_exact_terminal_card",
        lambda _store, **_kwargs: {
            "prompt": "怎样挑选陶瓷杯？",
            "category": "profile_interest",
            "evidence_bundle": {"source_url": "profile://interest/unit-1"},
            "source_locators": [["session", 0]],
        },
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("a clicked card must not read source history")

    monkeypatch.setattr(topic_controls, "recommendation_sources", fail)

    context = interface._starter_interaction_context(
        {"topic_starter": {"manifest_id": "manifest-1"}}, "怎样挑选陶瓷杯？",
    )

    assert context.startswith("Answer the clicked question")
    assert "profile://interest/unit-1" in context
    assert "user_source_constraints" not in context

def test_legacy_negative_preferences_are_scrubbed_once(tmp_path):
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

    store = TopicStarterStore(tmp_path / "scrub.db")
    state = history.empty_history_state()
    state["memory_sidecar"] = {
        "negative_preference_version": 2,
        "negative_preferences": [{
            "reason": "explicit_not_interested",
            "source_id": "unit-1",
            "context_scope": "global_new_chat",
            "project_id": None,
            "dismissed_at": 1.0,
        }],
    }
    history.save_history_state(store, state, now=10.0)
    assert store.record_event({
        "session_id": "session-1",
        "slate_id": "manifest-1",
        "event_type": "not_interested",
        "topic_id": "revision-1",
        "policy_id": "topic-starter-mvp-v4",
        "selection_mode": "personalized_dynamic",
        "propensity_reason": "not_estimated",
        "payload": {},
        "created_at": 5.0,
    })

    assert history.scrub_legacy_negative_preferences(store, now=11.0) is True
    scrubbed = history.load_latest_history_state(store)
    assert scrubbed["memory_sidecar"]["negative_preferences"] == []
    assert scrubbed["memory_sidecar"]["negative_preference_version"] == 0
    assert store.query_events(
        event_type="not_interested", policy_id="topic-starter-mvp-v4", limit=1,
    ) == []

    # The marker keeps later starts idle.
    assert history.scrub_legacy_negative_preferences(store, now=12.0) is False

def test_selection_candidates_no_longer_need_dismissal_flags():
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        _selection_candidates,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        InterestFeedback,
    )

    plan = {
        "candidate_id": "candidate-1",
        "category": "profile_recent_progress",
        "interest_summary_id": "interest-1",
        "identity_anchor": "anchor",
        "source_memory_unit_ids": [],
        "title": "标题",
        "prompt": "问题",
        "intent": "意图",
        "freshness_class": "recent_development",
        "evidence_bundle": {"content_boundary": {"text": "证据"}},
    }
    candidates = _selection_candidates(
        [plan],
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        feedback=InterestFeedback(now=0.0),
        news_plan=None,
    )

    assert candidates[0].candidate_id == "candidate-1"

