# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified ``observability`` config section and its legacy migration.

Team and single-agent tracing used to be configured by two top-level sections,
``team_observability`` and ``agent_observability``. Both fed the same
``ObservabilityConfig``, and OpenTelemetry allows one global ``TracerProvider``
per process, so the two exporter blocks could never take effect side by side:
whichever runtime initialized first won and the other block was silently
ignored. The only settings that are genuinely per runtime are the switches.

The canonical shape is therefore one shared exporter block with one switch per
runtime::

    observability:
      team:
        enabled: true
      agent:
        enabled: false
      exporter: file
      endpoint: http://localhost:4317
      ...

``migrate_legacy_observability_config`` rewrites a config carrying the legacy
sections into that shape. It runs inside ``migrate_config_from_template``
before the template merge, and ``resolve_observability_section`` applies it to
an in-memory copy at read time so a config the template migration skips (for
example under AgentOS) still resolves correctly.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from typing import Any, Literal

logger = logging.getLogger(__name__)

OBSERVABILITY_CONFIG_KEY = "observability"
LEGACY_TEAM_OBSERVABILITY_KEY = "team_observability"
LEGACY_AGENT_OBSERVABILITY_KEY = "agent_observability"

_OBSERVABILITY_SECTION_KEYS: tuple[str, ...] = (
    OBSERVABILITY_CONFIG_KEY,
    LEGACY_TEAM_OBSERVABILITY_KEY,
    LEGACY_AGENT_OBSERVABILITY_KEY,
)

ObservabilityScope = Literal["team", "agent"]
OBSERVABILITY_SCOPES: tuple[ObservabilityScope, ...] = ("team", "agent")

_LEGACY_KEY_BY_SCOPE: dict[ObservabilityScope, str] = {
    "team": LEGACY_TEAM_OBSERVABILITY_KEY,
    "agent": LEGACY_AGENT_OBSERVABILITY_KEY,
}

# Values the legacy template shipped for each section. A legacy value equal to
# its section's shipped default is treated as "not chosen by the operator", so
# an operator's edit in one section is not overridden by the untouched default
# left behind in the other one.
_LEGACY_SHARED_DEFAULTS: dict[str, Any] = {
    "exporter": "file",
    "endpoint": "http://localhost:4317",
    "sample_rate": 1.0,
    "redact_prompts": False,
    "redact_completions": False,
    "attribute_value_max_length": 10240,
    "langfuse_public_key": "",
    "langfuse_secret_key": "",
    "traces_dir": "",
    "file_retention_days": 7,
}
_LEGACY_SERVICE_NAME_DEFAULTS: dict[ObservabilityScope, str] = {
    "team": "jiuwenswarm",
    "agent": "jiuwenswarm-agent",
}

_legacy_warning_lock = threading.Lock()
_legacy_warning_emitted = False


def _legacy_default(scope: ObservabilityScope, key: str) -> Any:
    if key == "service_name":
        return _LEGACY_SERVICE_NAME_DEFAULTS[scope]
    return _LEGACY_SHARED_DEFAULTS.get(key)


def _merge_legacy_value(
    key: str,
    legacy_sections: Mapping[ObservabilityScope, Mapping[str, Any]],
    *,
    log_conflicts: bool,
) -> tuple[bool, Any]:
    """Pick the value one shared field takes when two legacy sections merge.

    Args:
        key: Shared field name, such as ``exporter``.
        legacy_sections: Legacy sections by scope; only those present.
        log_conflicts: Log a WARNING when both sections set conflicting
            values. The message names the field and the winning section only:
            values are never logged, since fields such as
            ``langfuse_secret_key`` hold credentials.

    Returns:
        ``(found, value)``. ``found`` is False when the field must be left
        unset so the template (or the runtime default) supplies it.
    """
    candidates = {
        scope: section[key] for scope, section in legacy_sections.items() if key in section
    }
    if not candidates:
        return False, None
    distinct = list({repr(value): value for value in candidates.values()}.values())
    if len(distinct) == 1:
        return True, distinct[0]

    customized = {
        scope: value
        for scope, value in candidates.items()
        if value != _legacy_default(scope, key)
    }
    if not customized:
        # Both sides kept their own shipped default (only service_name ships
        # different defaults); the new template default applies.
        return False, None
    customized_distinct = list({repr(value): value for value in customized.values()}.values())
    if len(customized_distinct) == 1:
        return True, customized_distinct[0]

    enabled_scopes = [
        scope for scope in customized if bool(legacy_sections[scope].get("enabled", False))
    ]
    winner: ObservabilityScope = enabled_scopes[0] if len(enabled_scopes) == 1 else "team"
    if log_conflicts:
        logger.warning(
            "observability config migration: %s differs between %s and %s; "
            "keeping the %s value",
            key,
            LEGACY_TEAM_OBSERVABILITY_KEY,
            LEGACY_AGENT_OBSERVABILITY_KEY,
            _LEGACY_KEY_BY_SCOPE[winner],
        )
    return True, customized[winner]


def migrate_legacy_observability_config(
    config: MutableMapping[str, Any],
    *,
    log_conflicts: bool = True,
) -> bool:
    """Fold ``team_observability`` / ``agent_observability`` into ``observability``.

    The legacy sections are removed. Values already present in the canonical
    section win; legacy values only fill keys it does not set. Each legacy
    ``enabled`` flag becomes ``observability.<scope>.enabled``. Shared fields
    that disagree between the two legacy sections resolve to the operator's
    edit over a shipped default, then to the enabled section, then to Team.

    Args:
        config: Whole config mapping, mutated in place.
        log_conflicts: Log one WARNING per conflicting shared field (field
            name and winning section only, never the values).

    Returns:
        True when the mapping changed.
    """
    legacy_sections: dict[ObservabilityScope, Mapping[str, Any]] = {}
    found_legacy = False
    for scope, legacy_key in _LEGACY_KEY_BY_SCOPE.items():
        if legacy_key not in config:
            continue
        found_legacy = True
        section = config.pop(legacy_key)
        if isinstance(section, Mapping):
            legacy_sections[scope] = section
    if not found_legacy:
        return False

    canonical = config.get(OBSERVABILITY_CONFIG_KEY)
    if not isinstance(canonical, MutableMapping):
        canonical = {}
        config[OBSERVABILITY_CONFIG_KEY] = canonical

    for scope, section in legacy_sections.items():
        if "enabled" not in section:
            continue
        switch = canonical.get(scope)
        if not isinstance(switch, MutableMapping):
            switch = {}
            canonical[scope] = switch
        if "enabled" not in switch:
            switch["enabled"] = bool(section["enabled"])

    shared_keys: list[str] = []
    for section in legacy_sections.values():
        for key in section:
            if key != "enabled" and key not in OBSERVABILITY_SCOPES and key not in shared_keys:
                shared_keys.append(str(key))
    for key in shared_keys:
        if key in canonical:
            continue
        found, value = _merge_legacy_value(key, legacy_sections, log_conflicts=log_conflicts)
        if found:
            canonical[key] = deepcopy(value)
    return True


def resolve_observability_section(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the effective ``observability`` section of one loaded config.

    A config still carrying the legacy sections is migrated on a copy, so the
    caller's mapping is never modified. The deprecation warning, and any
    conflict warnings from the migration, are logged once per process: this
    runs on every Team / Agent request, so repeating them would flood logs.

    Args:
        config: Whole loaded config.

    Returns:
        The canonical section as a plain dict (empty when absent).
    """
    view: dict[str, Any] = {}
    for key in _OBSERVABILITY_SECTION_KEYS:
        if key in config:
            view[key] = deepcopy(config[key])
    if LEGACY_TEAM_OBSERVABILITY_KEY in view or LEGACY_AGENT_OBSERVABILITY_KEY in view:
        migrate_legacy_observability_config(view, log_conflicts=_claim_legacy_warning())
    section = view.get(OBSERVABILITY_CONFIG_KEY)
    return dict(section) if isinstance(section, Mapping) else {}


def is_observability_scope_enabled(
    section: Mapping[str, Any],
    scope: ObservabilityScope,
) -> bool:
    """Return the explicit switch of one runtime (``team`` or ``agent``)."""
    switch = section.get(scope)
    return isinstance(switch, Mapping) and bool(switch.get("enabled", False))


def observability_export_settings(section: Mapping[str, Any]) -> dict[str, Any]:
    """Return the shared exporter settings, without the per-runtime switches."""
    return {key: value for key, value in section.items() if key not in OBSERVABILITY_SCOPES}


def _claim_legacy_warning() -> bool:
    """Return True for the first legacy read in this process, and log it then."""
    global _legacy_warning_emitted
    with _legacy_warning_lock:
        if _legacy_warning_emitted:
            return False
        _legacy_warning_emitted = True
    logger.warning(
        "config sections '%s' / '%s' are deprecated and were read as '%s' "
        "(team.enabled / agent.enabled plus one shared exporter block); "
        "they are rewritten automatically on the next config migration",
        LEGACY_TEAM_OBSERVABILITY_KEY,
        LEGACY_AGENT_OBSERVABILITY_KEY,
        OBSERVABILITY_CONFIG_KEY,
    )
    return True
