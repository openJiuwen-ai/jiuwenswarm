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
    assert switches.DEFAULT_PROMPT_SUGGESTION_ENABLED is False

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
    assert switches.is_prompt_suggestion_enabled(config) is False

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
    assert switches.is_prompt_suggestion_enabled(config) is False

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

def test_by_default_topics_follow_the_model_new_chats_use(chat_config):
    config = _resolved()
    assert config is not None
    assert (config.model, config.base_url, config.api_key) == (
        "chat-model", "https://chat.example/api/v3", "placeholder-test-key")
    # The entry leaves its window unset; the chain does not take the chat list's 256K default.
    assert config.context_window_tokens == 131072
    assert (config.effective_temperature, config.effective_top_p) == (0, 1)

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

    assert save_recommendation_model("follow_main") is True
    assert _resolved().model == "chat-model"

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

def test_a_project_card_has_no_pause_and_its_dismissal_takes_off_only_that_card_until_undone(tmp_path: Any) -> None:
    scope = ("project:project-a", "project-a")
    store = TopicStarterStore(tmp_path / "project-undo.sqlite3")
    _install_exact_exposure(store, scope=scope)
    queue = NewPageOutcomeQueue()
    assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == []

    assert queue.publish(_signal(outcome="interest_paused", scope=scope)) == "rejected"
    assert queue.publish(_signal(outcome="not_interested", scope=scope)) == "recorded"
    assert queue.publish(_signal(outcome="not_interested", scope=scope)) == "rejected"
    feedback = InterestFeedback.from_state(
        load_latest_history_state(store), context_scope=scope[0], project_id=scope[1], now=LIFECYCLE_NOW + 3,
    )
    [record] = load_latest_history_state(store)["memory_sidecar"]["negative_preferences"]
    assert record["dismissal_kind"] == "card" and "source_id" not in record
    # The project's unit and its other cards stay; only the dismissed question is off.
    assert feedback.dismissed_units == frozenset()
    sibling = {"intent": "causal recommendation evaluation", "title": "Which offline metric fits uplift?",
               "prompt": "Compare uplift metrics", "source_memory_unit_ids": ["memory-causal-evaluation"]}
    assert feedback.still_wanted(sibling) is True
    assert feedback.still_wanted({**sibling, "title": "How should causal recommendation be evaluated?"}) is False
    assert queue.publish(_signal(outcome="not_interested_undone", scope=scope)) == "recorded"
    assert queue.publish(_signal(outcome="not_interested_undone", scope=scope)) == "rejected"
    feedback = InterestFeedback.from_state(
        load_latest_history_state(store), context_scope=scope[0], project_id=scope[1], now=LIFECYCLE_NOW + 3,
    )
    assert feedback.dismissed_units == frozenset()
    assert feedback.version == 2
    assert queue.publish(_signal(outcome="not_interested", scope=scope)) == "recorded"

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
from gateway.channel_manager.base import RobotMessageRouter
from gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
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
    _give_way_to_feedback = TopicService._give_way_to_feedback
    # A recommendation model is in force, so a read does not reload the configuration.
    ark_config = object()
    def schedule_news_check(self, *_args, **_kwargs):
        return None

    def __init__(self, store: TopicStarterStore) -> None:
        self.store = store
        self._news_refresh_task = None
        # No replenishment runs here, so an undo or a pause has none to restart.
        self._runs: dict[str, Any] = {}
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
    ) -> dict[str, Any]:
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
    ) -> None:
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
    card_event_types = {"topic_click", "not_interested", "session_bound"}
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
                    else {"negative_feedback": False}
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
    assert service.successor_calls == [
        (GLOBAL_NEW_CHAT_SCOPE, None, "exposure-1")
    ]
    ws.request(
        "session-bound",
        "topic_starters.event",
        _ui_event(event_type="session_bound", session_id="real-chat-session"),
    )
    assert (await _wait_response(ws, "session-bound"))["payload"] == {
        "recorded": True
    }
    ws.request("ignored", "topic_starters.event", _ui_event(event_type="not_interested"))
    assert (await _wait_response(ws, "ignored"))["payload"] == {"recorded": True, "pausable": True}
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
    assert service.successor_calls == [
        (GLOBAL_NEW_CHAT_SCOPE, None, "exposure-1"),
        (GLOBAL_NEW_CHAT_SCOPE, None, "exposure-1")
    ]

    for event_type in ("topic_click", "not_interested"):
        stored = store.query_events(
            event_type=event_type,
            slate_id="exposure-1",
            topic_id="revision-1",
            policy_id=POLICY_ID,
        )
        assert len(stored) == 1
        assert stored[0]["payload"]["manifest_id"] == "manifest-1"
        assert stored[0]["payload"]["generation_batch_id"] == "batch-1"

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
            "negative_feedback": False,
        }

    ws.request(
        "tampered",
        "topic_starters.event",
        _ui_event(event_type="topic_click", batch_id="tampered"),
    )
    assert (await _wait_response(ws, "tampered"))["payload"] == {
        "recorded": False
    }
    assert len(service.successor_calls) == 2
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
async def test_webchannel_reports_unavailable_when_service_is_disabled(
    enabled_recommendation_consent,
) -> None:
    channel = WebChannel(
        WebChannelConfig(enabled=True), RobotMessageRouter()
    )
    channel.on_message(lambda _msg: False)
    _register_panel(channel, service=None, store=None, queue=None)
    ws = _OpenWebSocket("/ws?user_id=alice&session_id=visit-session")
    connection = asyncio.create_task(channel.handle_connection(ws, path=ws.path))

    ws.request(
        "get-unavailable",
        "topic_starters.get",
        {
            "session_id": "visit-session",
            "policy_id": POLICY_ID,
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
        },
    )

    response = await _wait_response(ws, "get-unavailable")
    assert response["ok"] is True
    assert response["payload"] == {
        "available": False,
        "reason": "topic_service_unavailable",
        "session_id": "visit-session",
        "policy_id": POLICY_ID,
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "project_id": None,
    }

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
async def test_dismissing_pausing_and_undoing_a_card_are_answered_and_refilled(
    tmp_path: Any,
    enabled_recommendation_consent,
) -> None:
    from jiuwenswarm.agents.harness.common.recommendation import topic_history as history
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import InterestFeedback

    store = TopicStarterStore(tmp_path / "dismiss-pause-undo.sqlite3")
    _install_manifest_and_card(store)
    # Two strikes written before a dismissal took off only its card stay as they were.
    _save_profile_with_dismissals(store, (CHANNEL_NOW - 200 * 86_400.0, CHANNEL_NOW - 100 * 86_400.0))
    service = _TopicServiceStub(store)
    queue = NewPageOutcomeQueue()
    assert queue.attach_sink(TopicStarterStoreOutcomeSink(store)) == []
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.on_message(lambda _msg: False)
    _register_panel(channel, service=service, store=store, queue=queue)
    ws = _OpenWebSocket("/ws?user_id=alice&session_id=visit-session")
    connection = asyncio.create_task(channel.handle_connection(ws, path=ws.path))

    def card_event(event_type: str) -> dict[str, Any]:
        event = _ui_event(event_type="not_interested")
        return {**event, "event_type": event_type}

    ws.request("exposure", "topic_starters.event", _ui_event(event_type="dynamic_exposure"))
    assert (await _wait_response(ws, "exposure"))["payload"] == {"recorded": True}
    replies = []
    for request_id, event_type in (
        ("dismiss", "not_interested"), ("pause", "interest_paused"), ("pause-again", "interest_paused"),
        ("undo-pause", "interest_pause_undone"), ("undo-dismiss", "not_interested_undone"),
        ("dismiss-again", "not_interested"),
    ):
        ws.request(request_id, "topic_starters.event", card_event(event_type))
        replies.append((await _wait_response(ws, request_id))["payload"])
    assert replies == [
        {"recorded": True, "pausable": True}, {"recorded": True}, {"recorded": False},
        {"recorded": True}, {"recorded": True}, {"recorded": True, "pausable": True},
    ]
    assert len(service.successor_calls) == 5
    state = history.load_latest_history_state(store)
    feedback = InterestFeedback.load(store, state, context_scope=GLOBAL_NEW_CHAT_SCOPE, project_id=None, now=CHANNEL_NOW)
    assert [item.status for item in feedback.permanent_directions] == []
    assert len(state["memory_sidecar"]["negative_preferences"]) == 2 + 5

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

def test_gateway_keeps_prepare_lane_when_search_is_unconfigured(
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
    assert product_service is None
    assert outcome_queue.pending_count == 0

def test_gateway_startup_seeds_product_lane() -> None:
    calls: list[tuple[str, str | None]] = []

    class PrepareLane:
        def schedule_seed(self) -> None:
            calls.append(("prepare", None))

    class ProductLane:
        def schedule_seed(self, run_id: str) -> None:
            calls.append(("product", run_id))

    run_id = "topic-starter-v6:1788112805"
    recommendation_host._seed_topic_starter_services(PrepareLane(), ProductLane(), run_id)

    assert calls == [("product", run_id)]

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
        assert 1 <= len(response["topics"]) <= 4
        assert response["selection_mode"] == "generic_dynamic"
        assert all(topic["title"] and topic["prompt"] for topic in response["topics"])
    finally:
        pass


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
        build_cold_start_cards,
    )

    now = datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp()
    cards = build_cold_start_cards(
        trigger_key={
            "local_date": "2026-10-21",
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
        on = service.serve(
            "session-new",
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
        )
        assert on["preparation_state"] == "preparing"
        assert on["topics"]
        await asyncio.wait_for(started.wait(), timeout=5)
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
    from gateway.app_gateway import sync_saved_topic_recommendation

    warnings: list[str] = []

    def record(message: str, *args: Any, **_kwargs: Any) -> None:
        warnings.append(message % args if args else message)

    async def reject(**_kwargs):
        return False, {"error": "AgentServer is unavailable", "code": "SERVICE_UNAVAILABLE"}

    monkeypatch.setattr("gateway.routing.e2a_proxy.fetch_agent_unary", reject)
    monkeypatch.setattr("gateway.app_gateway.logger.warning", record)
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
