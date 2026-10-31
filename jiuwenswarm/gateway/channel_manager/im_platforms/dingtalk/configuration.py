"""DingTalk app configuration: normalization, the default app and safe saving."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

_TEXT_FIELDS = ("name", "client_id", "client_secret", "persistent_agent_id", "robot_code", "api_base", "oapi_base")
_FILE_FIELDS = ("max_download_size", "download_timeout", "send_file_allowed", "enable_file_download", "workspace_dir")
# Keys that describe one app. In the legacy flat form they live directly under channels.dingtalk.
_APP_KEYS = ("name", "enabled", "client_id", "client_secret", "persistent_agent_id", "robot_code", "allow_from",
             "is_default")
# Ordinary-channel identity read by the existing cron fallback; it belongs to the default app only.
CRON_IDENTITY_KEYS = ("last_sender_id", "last_conversation_id", "last_conversation_type")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _allow_list(value: Any) -> list[str] | None:
    """The allowlist, or None when the configured value is not a list of strings."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return [item.strip() for item in value if item.strip()]


def allow_from_valid(app: Mapping[str, Any]) -> bool:
    """An empty list admits everyone, so a malformed allowlist must never be read as empty."""
    return _allow_list(app.get("allow_from")) is not None


def _normalize_app(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Give every field its declared type; YAML leaves empty keys as None."""
    app = dict(raw)
    app.pop("apps", None)
    for key in _TEXT_FIELDS:
        app[key] = _text(raw.get(key))
    allow_from = _allow_list(raw.get("allow_from"))
    # A malformed allowlist stays as written and makes the app invalid instead of admitting everyone.
    app["allow_from"] = raw.get("allow_from") if allow_from is None else allow_from
    enabled = raw.get("enabled")
    # An unset flag follows the channel startup rule: enabled once credentials exist.
    app["enabled"] = enabled if isinstance(enabled, bool) else bool(app["client_id"] and app["client_secret"])
    app["is_default"] = raw.get("is_default") is True
    return app


def _choose_default(apps: list[dict[str, Any]]) -> None:
    """The ordinary app is the default; otherwise only an app already marked as default stays default.

    Messages without an app identity, such as cron pushes, use the default app. A JM app
    never becomes the default by position, so deleting the ordinary app cannot silently
    move those messages to another robot.
    """
    ordinary = next((index for index, app in enumerate(apps) if not app["persistent_agent_id"]), None)
    marked = next((index for index, app in enumerate(apps) if app["is_default"]), None)
    default = ordinary if ordinary is not None else marked
    for index, app in enumerate(apps):
        app["is_default"] = index == default


def dingtalk_apps(raw: Any) -> list[dict[str, Any]]:
    """Read the app list, or the legacy single-app object, without mutating either."""
    if not isinstance(raw, Mapping):
        return []
    configured = raw.get("apps")
    if isinstance(configured, list):
        apps = [_normalize_app(app) for app in configured if isinstance(app, Mapping)]
    else:
        legacy = _normalize_app({key: raw[key] for key in _APP_KEYS if key in raw})
        if not legacy["client_id"]:
            return []
        legacy["is_default"] = True
        apps = [legacy]
    _choose_default(apps)
    return apps


def dingtalk_channel_settings(raw: Any, *, api_base: str, oapi_base: str) -> list[dict[str, Any]]:
    """Typed settings for each DingTalk channel to start; skipped apps are left out.

    Disabled, incomplete and duplicated apps do not start, and only one ordinary app is
    started. ``api_base`` and ``oapi_base`` are the defaults when neither the app nor the
    shared section names a domain. File settings use app values before shared values;
    unset values keep the channel model's defaults.
    """
    shared = raw if isinstance(raw, Mapping) else {}
    apps = dingtalk_apps(raw)
    ids = [app["client_id"] for app in apps]
    settings = []
    ordinary_started = False
    for app in apps:
        if not app["enabled"] or not app["client_id"] or not app["client_secret"] or ids.count(app["client_id"]) != 1:
            continue
        if not allow_from_valid(app):
            logger.warning("[DingTalk] app not started: allow_from must be a list of staff IDs: app_id=%s",
                           app["client_id"])
            continue
        if not app["persistent_agent_id"]:
            if ordinary_started:
                continue
            ordinary_started = True
        channel_settings = {
            "enabled": True,
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
            "agent_id": app["persistent_agent_id"],
            "robot_code": app["robot_code"],
            "is_default": app["is_default"],
            "allow_from": app["allow_from"],
            "api_base": app["api_base"] or _text(shared.get("api_base")) or api_base,
            "oapi_base": app["oapi_base"] or _text(shared.get("oapi_base")) or oapi_base,
        }
        for key in _FILE_FIELDS:
            value = app.get(key)
            if value is None:
                value = shared.get(key)
            if value is not None:
                channel_settings[key] = value
        settings.append(channel_settings)
    return settings


def dingtalk_section_view(raw: Any) -> dict[str, Any]:
    """The section as the settings page edits it: shared keys plus the normalized app list."""
    section = (
        {key: value for key, value in raw.items() if key not in _APP_KEYS and key != "apps"}
        if isinstance(raw, Mapping) else {}
    )
    section["apps"] = dingtalk_apps(raw)
    return section


def merge_dingtalk_config(current: Any, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Apply an apps[] edit and return the whole channels.dingtalk section to save.

    Each app is matched by client_id. A secret, binding or robot code the edit does not
    send is kept. Changing the default app's client_id is a different robot, so the
    ordinary cron identity is dropped instead of being reused through the new robot.
    """
    current = dict(current) if isinstance(current, Mapping) else {}
    if not isinstance(changes.get("apps"), list):
        raise ValueError("apps must be a list")
    previous = {app["client_id"]: app for app in dingtalk_apps(current) if app["client_id"]}
    old_default = next((app["client_id"] for app in previous.values() if app["is_default"]), None)
    apps = []
    for raw in changes["apps"]:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid DingTalk app")
        client_id = _text(raw.get("client_id"))
        if not client_id:
            raise ValueError("client_id is required")
        old = previous.get(client_id, {})
        # Merge onto the saved app before deriving anything, so an omitted field keeps its value.
        # The settings form does not send bindings; an empty secret means "unchanged".
        combined = {**old, **raw}
        if not _text(raw.get("client_secret")):
            combined["client_secret"] = old.get("client_secret", "")
        combined["is_default"] = old.get("is_default", False)
        if not old and "allow_from" not in raw:
            # An omitted allowlist means "unchanged"; a new app has nothing to keep, and defaulting
            # it to empty would admit everyone, for example after a client_id change.
            raise ValueError("allow_from is required for a new DingTalk app")
        app = _normalize_app(combined)
        # Only a submitted allowlist is checked. An untouched malformed one stays as written, so
        # its app still does not start and an unrelated edit cannot turn it into "everyone".
        if "allow_from" in raw and not allow_from_valid(app):
            raise ValueError("allow_from must be a list of staff IDs")
        apps.append(app)
    ids = [app["client_id"] for app in apps]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate DingTalk client_id")
    if sum(not app["persistent_agent_id"] for app in apps) > 1:
        raise ValueError("only one ordinary DingTalk app is supported; bind other apps to JiuwenMate colleagues")
    _choose_default(apps)
    section = {key: value for key, value in current.items() if key not in _APP_KEYS and key != "apps"}
    section["apps"] = apps
    new_default = next((app["client_id"] for app in apps if app["is_default"]), None)
    if new_default is None or new_default != old_default:
        for key in CRON_IDENTITY_KEYS:
            section.pop(key, None)
    return section


def update_dingtalk_section(
    build: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    validate: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Apply an apps[] edit to the section on disk and return the section that was saved.

    ``build`` receives the stored section and returns the edit. Reading, merging, validating
    and writing happen inside the config write lock, so neither a concurrent save nor an
    ordinary channel's ``last_*`` update is lost. A key dropped by the merge leaves the file.
    """
    from jiuwenswarm.common.config import update_config

    saved: dict[str, Any] = {}

    def replace(data: Any) -> Any:
        channels = data.get("channels")
        if not isinstance(channels, dict):
            channels = {}
            data["channels"] = channels
        stored = channels.get("dingtalk")
        section = merge_dingtalk_config(stored, build(stored if isinstance(stored, Mapping) else {}))
        if validate is not None:
            validate(section)
        if not isinstance(stored, dict):
            channels["dingtalk"] = dict(section)
        else:
            for key in [key for key in stored if key not in section]:
                del stored[key]
            for key, value in section.items():
                stored[key] = value
        saved.update(section)
        return data

    update_config(replace)
    return saved
