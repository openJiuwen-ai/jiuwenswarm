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
    from jiuwenswarm.server.runtime.session import session_history
    controls.save_recommendation_controls(enabled=True, include_history=True, now=2.0)
    records = _records()[1:]
    state = history.empty_history_state()
    state["history_ledger"] = history.rescan_history(None, {"session": records}, {}).ledger
    state["memory"]["units"] = [{"core": {"preference_summary": "保留独立事项"}, "companion": {"unit_id": "unit-1", "canonical_source_locators": [["session", 0]]}}]
    store = TopicStarterStore(tmp_path / "profile.db")
    history.save_history_state(store, state, now=4.0)
    monkeypatch.setattr(session_history, "load_history_records", lambda sid: records)
    profile = controls.inspect_recommendation_profile(store, now=5.0)
    assert profile["units"][0]["sources"][0]["content"] == "new independent request"
    records[0] = {**records[0], "content": "rewritten source"}
    assert controls.inspect_recommendation_profile(store, now=5.0)["units"] == []
    controls.save_recommendation_controls(enabled=False, deleted=True, now=5.0)
    assert controls.recommendation_sources(store, [["session", 0]]) == []

def test_permanent_directions_are_listed_and_can_be_restored(tmp_path, settings, monkeypatch):
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import InterestFeedback
    from jiuwenswarm.server.runtime.session import session_history
    monkeypatch.setattr(session_history, "load_history_records", lambda sid: [])
    day = 86_400.0

    def dismissal(unit_id, at):
        return {"context_scope": "global_new_chat", "project_id": None, "direction": "一锅做荤素搭配",
                "source_id": unit_id, "aliases": [], "anchors": [], "reason": "explicit_not_interested",
                "dismissed_at": at, "evidence_session_ids": ["session-a"]}

    state = history.empty_history_state()
    state["memory"]["units"] = [{"core": {"preference_summary": "工作日一口锅做荤素搭配的晚饭"},
                                 "companion": {"unit_id": "unit-0023", "canonical_source_locators": [["session-a", 0]],
                                               "evidence_ids": ["session-a:req-0:user"]}}]
    state["memory_sidecar"] = {"negative_preference_version": 3, "negative_preferences": [
        dismissal("unit-0023", at) for at in (10 * day, 20 * day, 30 * day)]}
    store = TopicStarterStore(tmp_path / "permanent.db")
    history.save_history_state(store, state, now=31 * day)

    profile = controls.inspect_recommendation_profile(store, now=40 * day)
    assert profile["permanent_directions"] == [
        {"direction_id": "unit|unit-0023", "summary": "工作日一口锅做荤素搭配的晚饭", "since": 30 * day}]

    assert controls.restore_dismissed_direction(store, "unit|unit-0023", now=41 * day) is True
    assert controls.restore_dismissed_direction(store, "unit|unit-0023", now=42 * day) is False
    assert controls.inspect_recommendation_profile(store, now=43 * day)["permanent_directions"] == []
    restored = history.load_latest_history_state(store)
    # The restore is one more record; the earlier ones stay as they were.
    assert restored["memory_sidecar"]["negative_preference_version"] == 4
    assert restored["memory_sidecar"]["negative_preferences"][:3] == state["memory_sidecar"]["negative_preferences"]
    feedback = InterestFeedback.from_state(restored, context_scope="global_new_chat", project_id=None, now=43 * day)
    assert feedback.dismissed_units == frozenset()
    # The count starts over: the next dismissal hides the direction for seven days, not for good.
    restored["memory_sidecar"]["negative_preferences"].append(dismissal("unit-0023", 50 * day))
    again = InterestFeedback.from_state(restored, context_scope="global_new_chat", project_id=None, now=57 * day)
    assert again.status_of({"unit-0023"}, ()) == "reduced"

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
    collect_user_evidence,
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

def _not_interested_event(exposure_id: str, at: float) -> dict[str, Any]:
    return _ledger_event(
        event_type="not_interested",
        slate_id=exposure_id,
        topic_id="revision-1",
        session_id="new-page-session",
        created_at=at,
        payload={
            "manifest_id": "manifest-1",
            "generation_batch_id": "batch-1",
            "exposure_id": exposure_id,
            "card_revision_id": "revision-1",
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "reason": "explicit_not_interested",
        },
    )

def test_upgrade_times_earlier_dismissals_from_the_ledger_and_follows_them_into_the_rebuilt_profile(
    tmp_path: Any,
) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
        migrate_dismissal_records,
    )

    day = 86_400.0
    store = TopicStarterStore(tmp_path / "upgrade.sqlite3")
    _install_exact_exposure(store)
    _install_exposure(store, "exposure-2")
    written = {
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "project_id": None,
        "direction": "causal recommendation evaluation",
        "source_id": "memory-causal-evaluation",
        "aliases": [
            "How should causal recommendation be evaluated?",
            "Compare causal recommendation evaluation protocols",
        ],
        "anchors": [],
        "reason": "explicit_not_interested",
    }
    # A dismissal the ledger has no event for keeps its meaning as written.
    unknown = {**written, "source_id": "memory-unknown", "direction": "unknown", "aliases": []}

    def profile(units: dict[str, list[str]], preferences: list[dict[str, Any]]) -> dict[str, Any]:
        state = empty_history_state()
        state["memory"]["units"] = [{
            "core": {"preference_summary": f"summary of {unit_id}"},
            "companion": {
                "unit_id": unit_id,
                "canonical_source_locators": [[session, 0] for session in sessions],
                "evidence_ids": [f"{session}:req-0:user" for session in sessions],
            },
        } for unit_id, sessions in units.items()]
        state["memory_sidecar"] = {
            "negative_preference_version": len(preferences), "negative_preferences": preferences,
        }
        return state

    save_history_state(store, profile({"memory-causal-evaluation": ["session-a", "session-b"]}, []), now=LIFECYCLE_NOW - day)
    # The same card dismissed on two visits was written once by the earlier deduplication.
    for exposure_id, at in (("exposure-1", LIFECYCLE_NOW), ("exposure-2", LIFECYCLE_NOW + day)):
        assert store.record_event(_not_interested_event(exposure_id, at))
    save_history_state(
        store,
        profile({"memory-causal-evaluation": ["session-a", "session-b"]}, [written, unknown]),
        now=LIFECYCLE_NOW + day,
    )
    # A later rebuild moved those conversations into a unit with a new ID.
    save_history_state(
        store,
        profile({"unit-rebuilt": ["session-a", "session-b", "session-c"]}, [written, unknown]),
        now=LIFECYCLE_NOW + 2 * day,
    )

    migrate_dismissal_records(store, now=LIFECYCLE_NOW + 3 * day)
    migrated = load_latest_history_state(store)
    assert migrated is not None
    preferences = migrated["memory_sidecar"]["negative_preferences"]
    assert [item.get("dismissed_at") for item in preferences] == [LIFECYCLE_NOW, None, LIFECYCLE_NOW + day]
    assert preferences[0]["evidence_session_ids"] == ["session-a", "session-b"]
    assert preferences[1] == unknown
    feedback = InterestFeedback.from_state(
        migrated, context_scope=GLOBAL_NEW_CHAT_SCOPE, project_id=None, now=LIFECYCLE_NOW + 3 * day,
    )
    assert feedback.status_of({"unit-rebuilt"}, ()) == "hidden"
    # Two strikes: hidden for thirty days from the second.
    assert InterestFeedback.from_state(
        migrated, context_scope=GLOBAL_NEW_CHAT_SCOPE, project_id=None, now=LIFECYCLE_NOW + 31 * day - 1,
    ).status_of({"unit-rebuilt"}, ()) == "hidden"

    # A later start does not read the dismissal ledger again, though one record stays untimed, and writes nothing.
    migrated_version = migrated["version"]
    ledger_reads: list[str | None] = []
    query_events = store.query_events

    def counted(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        ledger_reads.append(kwargs.get("event_type"))
        return query_events(*args, **kwargs)

    store.query_events = counted  # type: ignore[method-assign]
    migrate_dismissal_records(store, now=LIFECYCLE_NOW + 4 * day)
    assert "not_interested" not in ledger_reads
    assert load_latest_history_state(store)["version"] == migrated_version

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
    from e2a.models import E2AResponse
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.recommendation_host import RecommendationHost

    host = RecommendationHost()
    host.store = store
    host.topic_service = service
    host.topic_outcome_queue = queue
    host._services_built = True
    server = AgentWebSocketServer()
    host.bind_active_requests(server._recommendation_active_request_ids)
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
from jiuwenswarm.agents.harness.common.recommendation.topic_prepare import (
    TopicPrepareService,
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

    prepare_service, product_service, outcome_queue = (
        recommendation_host._build_topic_starter_services(store)
    )

    assert isinstance(prepare_service, TopicPrepareService)
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

    prepare_service, product_service, _outcome_queue = (
        recommendation_host._build_topic_starter_services(store)
    )

    assert isinstance(prepare_service, TopicPrepareService)
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
    prepare = host.topic_prepare_service
    queue = host.topic_outcome_queue
    service = host.topic_service
    assert host.topic_service.on_prepared is not None

    monkeypatch.setenv("FREE_SEARCH_DDG_ENABLED", "true")
    monkeypatch.setenv("FREE_SEARCH_BING_ENABLED", "true")
    host.ensure_services()
    assert host.topic_service is service
    assert host.topic_prepare_service is prepare
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
    assert work["available"] is True
    assert design["available"] is True
    assert work["work_mode"] == "work"
    assert design["work_mode"] == "design"
    assert work["manifest_id"] != design["manifest_id"]


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


def test_cold_start_prefers_live_timely_corpus_cards():
    from datetime import datetime, timezone

    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        build_cold_start_cards,
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
            "corpus_version": "topic-starter-corpus-v1",
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

        design = service.serve(
            "session-design",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode="design",
        )
        assert any(
            topic["source_starter_id"] == clicked["source_starter_id"]
            for topic in design["topics"]
        )
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

    await recommendation_host._shutdown_topic_starter_services(PrepareLane(), None)

    assert calls == ["prepare"]


def test_a_malformed_coverage_entry_drops_the_whole_receipt(tmp_path: Path) -> None:
    """One entry of the wrong shape invalidates the receipt instead of being trusted."""

    from jiuwenswarm.agents.harness.common.recommendation import topic_collector
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        POLICY_ID,
        SESSION_SUMMARY_COVERAGE_EVENT_TYPE,
        SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION,
    )

    store = TopicStarterStore(tmp_path / "coverage.db")
    payload = {
        "schema_version": SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION,
        "input_snapshot_id": "snapshot-1",
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "summary_config_version": "summary-config-1",
        "sessions": ["not-an-entry"],
    }
    assert (
        store.record_event(
            {
                "session_id": "topic-starter-session-summary",
                "slate_id": GLOBAL_NEW_CHAT_SCOPE,
                "event_type": SESSION_SUMMARY_COVERAGE_EVENT_TYPE,
                "topic_id": "snapshot-1|summary-config-1",
                "policy_id": POLICY_ID,
                "payload": payload,
                "created_at": 10.0,
            }
        )
        is True
    )

    assert (
        topic_collector._covered_standalone_session_summaries(store, page_size=50)
        == ()
    )


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
    from e2a.models import E2AResponse
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
