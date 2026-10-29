# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local recommendation consent and history ingestion boundary."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm.utils.provider_utils import (
    is_openai_account_provider,
)

from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig,
)
from jiuwenswarm.common.auth.model_catalog import is_login_model
from jiuwenswarm.common.config import (
    get_available_models,
    get_config,
    get_default_models,
    update_config,
)
from jiuwenswarm.common.context_window import parse_positive_int
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)

FOLLOW_MAIN_MODEL = "follow_main"
# The topic chain's window for a chosen model whose entry leaves it unset.
RECOMMENDATION_CONTEXT_WINDOW_TOKENS = 131_072

# The consent boundary and the deletion epoch are state, not configuration: they live beside the
# recommendation data, where they outlive `TopicStarterStore.delete_recommendation_data()` -- the
# epoch has to survive the deletion it marks.
_CONSENT_DB_NAME = "recommendation.db"
_CONSENT_TABLE = "recommendation_consent_state"
_CONSENT_BUSY_TIMEOUT_SECONDS = 5.0
# sqlite3 reports a store that predates the row as this, with no error code to match on.
_MISSING_TABLE = "no such table"
# Retired `recommendation.enabled` wordings whose consent the one-off migration still honours.
_LEGACY_ACCEPTED_CONSENT_VERSIONS = frozenset(
    {"recommendation-consent-v1", "recommendation-consent-v2"}
)


def _unset_consent() -> dict[str, Any]:
    """A fresh "nothing was ever recorded" state; callers may edit what they get."""

    return {"history_after": None, "deleted_at": None}


def _recorded_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _consent_db_path(store: Any = None) -> Path:
    """The file the consent row shares with the recommendation data it governs."""

    path = getattr(store, "path", None)
    return Path(path) if path is not None else get_agent_workspace_dir() / _CONSENT_DB_NAME


def _consent_state(store: Any = None) -> dict[str, Any]:
    """The recorded boundary and epoch, ``None`` when there is nothing to read.

    A read never materialises the database and never raises: a missing, locked or damaged store
    degrades to "nothing recorded", which hides history rather than exposing unconsented turns.
    """

    path = _consent_db_path(store)
    try:
        if not path.exists():
            return _unset_consent()
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                f"SELECT history_after, deleted_at FROM {_CONSENT_TABLE} WHERE id = 1"
            ).fetchone()
    except sqlite3.OperationalError as exc:
        # The row is created on the first opt-in, so a store that predates it is normal.
        if _MISSING_TABLE not in str(exc):
            logger.warning("[Recommendation] consent state unreadable at %s: %s", path, exc)
        return _unset_consent()
    except Exception as exc:  # noqa: BLE001 - a damaged store must not break the caller
        logger.warning("[Recommendation] consent state unreadable at %s: %s", path, exc)
        return _unset_consent()
    if row is None:
        return _unset_consent()
    return {"history_after": _recorded_float(row[0]), "deleted_at": _recorded_float(row[1])}


class RecommendationConsentStore:
    """The consent boundary and deletion epoch, one mutable row beside the recommendation data.

    A sibling of `TopicStarterStore` on the same file rather than a third table on it: the row
    must survive `delete_recommendation_data()`, which the deletion epoch is recorded against.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_CONSENT_TABLE} (
                    id            INTEGER PRIMARY KEY CHECK (id = 1),
                    history_after REAL,
                    deleted_at    REAL,
                    updated_at    REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=_CONSENT_BUSY_TIMEOUT_SECONDS,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(_CONSENT_BUSY_TIMEOUT_SECONDS * 1000)}")
        return connection

    def save(self, *, history_after: float | None, deleted_at: float | None,
             now: float) -> dict[str, Any]:
        """Record the boundary and epoch as one write, so the two cannot drift apart."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                INSERT INTO {_CONSENT_TABLE} (id, history_after, deleted_at, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    history_after = excluded.history_after,
                    deleted_at    = excluded.deleted_at,
                    updated_at    = excluded.updated_at
                """,
                (history_after, deleted_at, now),
            )
        return {"history_after": history_after, "deleted_at": deleted_at}


def load_recommendation_controls(store: Any = None) -> dict[str, Any]:
    """The switches as resolved, plus the recorded consent boundary and deletion epoch."""

    from jiuwenswarm.agents.harness.common.recommendation.switches import (
        is_topic_recommendation_enabled,
    )

    config = get_config()
    value = config.get("recommendation", {})
    result = dict(value) if isinstance(value, dict) else {}
    result["enabled"] = is_topic_recommendation_enabled(config)
    result.update(_consent_state(store))
    return result


def save_recommendation_controls(*, enabled: bool, include_history: bool = False,
                                 deleted: bool = False, now: float,
                                 store: Any = None) -> dict[str, Any]:
    """Record the consent boundary, and on deletion the epoch that retires its predecessors.

    Writes no configuration: the switches have one writer, the settings-panel path.
    """

    current = _consent_state(store)
    boundary = current["history_after"]
    if deleted:
        boundary = now
    elif enabled and include_history:
        boundary = 0.0
    elif enabled and boundary is None:
        boundary = now
    epoch = now if deleted else current["deleted_at"]
    RecommendationConsentStore(_consent_db_path(store)).save(
        history_after=boundary, deleted_at=epoch, now=now,
    )
    return {**load_recommendation_controls(store), "enabled": enabled,
            "history_after": boundary, "deleted_at": epoch}


def freeze_consent_boundary(now: float) -> None:
    """Freeze the history boundary when topic recommendation is first turned on.

    Kept on the same path that flips the switch: a boundary recorded afterwards would leave a
    window in which the feature reads as on while every recorded turn is still hidden.
    """

    current = _consent_state()
    if current["history_after"] is None:
        RecommendationConsentStore(_consent_db_path()).save(
            history_after=now, deleted_at=current["deleted_at"], now=now,
        )


def migrate_legacy_recommendation_switches() -> bool:
    """Retire ``recommendation.enabled`` onto the two switches it used to drive.

    The retired key drove both switches and only counted after an accepted consent wording, so a
    config carrying it without one migrates to the code default. The split keys are written
    explicitly: the template merge that follows would otherwise add its own ``null`` and the
    user's choice would be read as "unset".
    """

    from jiuwenswarm.agents.harness.common.recommendation.switches import (
        resolve_recommendation_switch,
    )

    section = get_config().get("recommendation")
    if not isinstance(section, dict) or "enabled" not in section:
        return False
    legacy = (
        resolve_recommendation_switch(section.get("enabled"), False)
        if section.get("consent_version") in _LEGACY_ACCEPTED_CONSENT_VERSIONS
        else False
    )

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        target = config.get("recommendation")
        if isinstance(target, dict):
            for key in ("topic_enabled", "prompt_enabled"):
                # A switch the user already chose wins; the template's ``null`` does not.
                if target.get(key) is None:
                    target[key] = legacy
            target.pop("enabled", None)
            target.pop("consent_version", None)
        return config

    update_config(mutate)
    logger.info("[Recommendation] migrated the retired 'enabled' key to the split switches")
    return True


def migrate_recommendation_consent_state() -> bool:
    """Move the consent boundary and deletion epoch out of config.yaml into the store.

    Also drops the retired write clock: nothing reads it, and leaving it behind would keep the
    configuration looking as though it still carried state.
    """

    section = get_config().get("recommendation")
    if not isinstance(section, dict):
        return False
    if not {"history_after", "deleted_at", "updated_at"} & set(section):
        return False
    boundary = _recorded_float(section.get("history_after"))
    epoch = _recorded_float(section.get("deleted_at"))
    if boundary is not None or epoch is not None:
        current = _consent_state()
        try:
            RecommendationConsentStore(_consent_db_path()).save(
                history_after=boundary if boundary is not None else current["history_after"],
                deleted_at=epoch if epoch is not None else current["deleted_at"],
                now=time.time(),
            )
        except Exception as exc:  # noqa: BLE001 - keep the config keys rather than lose the state
            logger.warning("[Recommendation] consent state not migrated: %s", exc)
            return False

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        target = config.get("recommendation")
        if isinstance(target, dict):
            target.pop("history_after", None)
            target.pop("deleted_at", None)
            target.pop("updated_at", None)
        return config

    update_config(mutate)
    return True


def _model_name(entry: dict[str, Any]) -> str:
    return str((entry.get("model_client_config") or {}).get("model_name") or "").strip()


def _unusable_reason(entry: dict[str, Any]) -> str | None:
    """Why the topic chain cannot call a chat model: it sends OpenAI chat completions with the key in config."""
    provider = str((entry.get("model_client_config") or {}).get("client_provider") or "")
    if entry.get("is_free"):
        return "free"
    if is_login_model(entry) or is_openai_account_provider(provider):
        return "login_required"
    if provider != "OpenAI":
        return "incompatible"
    return None


def _chosen_entry(config: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The entry chosen in settings, or None to follow the main model (also when the chosen one was removed)."""
    section = config.get("recommendation")
    model_id = str(section.get("model") or "") if isinstance(section, dict) else ""
    if model_id in ("", FOLLOW_MAIN_MODEL):
        return None
    return next((entry for entry in entries if str(entry.get("model_id") or "") == model_id), None)


def _main_model(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The model a new chat uses by default: the default entry of the first listed model name."""
    if not entries:
        return None
    name = _model_name(entries[0])
    return next((entry for entry in entries if _model_name(entry) == name and entry.get("is_default") is True),
                entries[0])


def recommendation_model_options(auth_session: str | None) -> dict[str, Any]:
    """The chat models the topic model can be chosen from, each with why it cannot be when it cannot."""
    config = get_config()
    entries = [entry for entry in get_available_models(config, auth_session) if _model_name(entry)]
    chosen = _chosen_entry(config, entries)
    return {
        "selected_model": str(chosen["model_id"]) if chosen is not None else FOLLOW_MAIN_MODEL,
        "model_options": [{"model_id": str(entry.get("model_id") or ""),
                           "name": str(entry.get("alias") or "") or _model_name(entry),
                           "disabled_reason": _unusable_reason(entry)} for entry in entries],
    }


def save_recommendation_model(model: str) -> bool:
    """Keep the topic model choice: ``follow_main`` or the ID of a configured model the chain can call."""
    if model != FOLLOW_MAIN_MODEL:
        entries = [entry for entry in get_default_models(get_config()) if _model_name(entry)]
        entry = next((entry for entry in entries if str(entry.get("model_id") or "") == model), None)
        if entry is None or _unusable_reason(entry) is not None:
            return False

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        section = config.get("recommendation")
        if not isinstance(section, dict):
            section = config["recommendation"] = {}
        section["model"] = model
        return config
    update_config(mutate)
    return True


def recommendation_model_config(environ: Any = None) -> ArkBackgroundConfig | None:
    """The topic chain's model: a complete environment override, else the chat model chosen in settings.

    The override applies only when ``ARK_API_KEY``, ``ARK_MODEL``, ``ARK_BASE_URL`` and
    ``ARK_CONTEXT_WINDOW_TOKENS`` are all set. Otherwise the choice is ``follow_main`` or one
    configured chat model. Name, endpoint, key and window come from that entry. None when
    that model cannot be called this way.
    """
    override = ArkBackgroundConfig.from_environ(environ)
    if override is not None:
        return override
    config = get_config()
    entries = [entry for entry in get_default_models(config) if _model_name(entry)]
    entry = _chosen_entry(config, entries) or _main_model(entries)
    if entry is None or _unusable_reason(entry) is not None:
        return None
    client = entry.get("model_client_config") or {}
    api_key = str(client.get("api_key") or "").strip()
    base_url = str(client.get("api_base") or "").strip()
    if not api_key or not base_url:
        return None
    return ArkBackgroundConfig(
        api_key=api_key,
        model=_model_name(entry),
        base_url=base_url,
        context_window_tokens=parse_positive_int((entry.get("model_config_obj") or {}).get("context_window"))
        or RECOMMENDATION_CONTEXT_WINDOW_TOKENS,
    )


def permitted_at(timestamp: Any, control: dict[str, Any]) -> bool:
    """Whether a request made at ``timestamp`` is inside the recommendation consent boundary."""
    boundary = control.get("history_after")
    return boundary == 0 or (boundary is not None and type(timestamp) in (int, float) and timestamp > boundary)


def permitted_recommendation_history(
    records: list[dict[str, Any]], control: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Hide unconsented requests without changing physical source ordinals.

    A turn another Session's Agent or a Heartbeat started is hidden too: the person did not
    ask it. A caller filtering many sessions at once passes the controls it already loaded.
    """
    from jiuwenswarm.server.runtime.session.session_history import (
        is_agent_initiated_record,
    )
    if control is None:
        control = load_recommendation_controls()
    agent_requests = {
        str(row.get("request_id") or "") for row in records
        if row.get("role") in {"user", "human"} and is_agent_initiated_record(row)
    }
    whole_history = control.get("history_after") == 0
    if whole_history and not agent_requests:
        return records
    allowed_requests = {
        str(row.get("request_id") or "") for row in records
        if row.get("role") in {"user", "human"} and permitted_at(row.get("timestamp"), control)
    }

    def kept(request_id: str) -> bool:
        return request_id not in agent_requests and (whole_history or request_id in allowed_requests)

    return [row if kept(str(row.get("request_id") or "")) else {
        "role": "recommendation_history_excluded", "request_id": row.get("request_id"),
        "id": row.get("id"), "timestamp": row.get("timestamp"),
    } for row in records]


def recommendation_sources(store: Any, locators: list[Any]) -> list[dict[str, Any]]:
    """Resolve only unchanged, currently permitted completed user records."""
    from jiuwenswarm.server.runtime.session.session_history import (
        load_history_records,
        project_completed_session_records,
    )

    from . import topic_history as history
    state = history.load_latest_history_state(store) or {}
    witnesses = {}
    for sid, session in state.get("history_ledger", {}).get("sessions", {}).items():
        for entry in session.get("entries", []):
            if not entry.get("retracted"):
                witnesses[(sid, entry["ordinal"])] = entry.get("witness")
    completed = {}
    sources = []
    for sid, ordinal in locators:
        if sid not in completed:
            completed[sid] = project_completed_session_records(
                sid,
                permitted_recommendation_history(load_history_records(sid)),
                allow_gaps=True,
            )
        rows = completed[sid]
        if ordinal >= len(rows):
            return []
        if rows[ordinal].get("role") not in {"user", "human"}:
            return []
        if witnesses.get((sid, ordinal)) != history.record_witness(rows[ordinal], True):
            return []
        sources.append({"session_id": sid, "ordinal": ordinal,
                        "event_id": rows[ordinal].get("id"), "content": rows[ordinal].get("content", "")})
    return sources


def _global_feedback(store: Any, state: dict[str, Any], now: float) -> Any:
    from .topic_product import GLOBAL_NEW_CHAT_SCOPE, InterestFeedback
    return InterestFeedback.load(store, state, context_scope=GLOBAL_NEW_CHAT_SCOPE, project_id=None, now=now)


def inspect_recommendation_profile(store: Any, *, now: float) -> dict[str, Any]:
    from . import topic_history as history
    state = history.load_latest_history_state(store) or {}
    units = []
    for unit in state.get("memory", {}).get("units", []):
        sources = recommendation_sources(store, unit.get("companion", {}).get("canonical_source_locators", []))
        if sources:
            units.append({"unit_id": unit["companion"]["unit_id"],
                          "summary": unit["core"]["preference_summary"], "sources": sources})
    summaries = {unit["companion"]["unit_id"]: unit["core"]["preference_summary"]
                 for unit in state.get("memory", {}).get("units", [])}
    # Only the global page has directions that become permanent; a project dismissal takes off one card.
    permanent = [
        {
            "direction_id": direction.direction_id,
            "summary": (
                direction.direction_anchor
                or summaries.get(direction.unit_id)
                or direction.records[-1].get("direction", "")
            ),
            "since": direction.permanent_since,
        }
        for direction in _global_feedback(store, state, now).permanent_directions
    ]
    return {"units": units, "semantic_version": state.get("memory", {}).get("semantic_version", 0),
            "permanent_directions": permanent}


def restore_dismissed_direction(store: Any, direction_id: str, *, now: float) -> bool:
    """Lift a permanent global direction with one more record; it may return at the next regeneration."""
    from . import topic_history as history
    state = history.load_latest_history_state(store)
    if state is None:
        return False
    feedback = _global_feedback(store, state, now)
    direction = next((item for item in feedback.permanent_directions if item.direction_id == direction_id), None)
    if direction is None:
        return False
    sidecar = state.setdefault("memory_sidecar", {})
    sidecar["negative_preferences"] = [*(sidecar.get("negative_preferences") or []),
                                       feedback.restore_record(direction, at=now)]
    sidecar["negative_preference_version"] = int(sidecar.get("negative_preference_version") or 0) + 1
    history.save_history_state(store, state, now=now)
    return True
