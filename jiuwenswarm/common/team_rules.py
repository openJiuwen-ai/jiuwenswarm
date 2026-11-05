# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Group team rules settings (``config.yaml`` ``team_rules``).

The delivery policy, the supervisor call and the safe interrupt live in
agent-core (``TeamAgentSpec.team_rules``). This module maps the settings page
to ``config.yaml`` and builds the ``team_rules`` dict handed to the team spec.

The section only holds ``mode``, written the same way as the browser decision
mode:

* ``off``: no supervisor, messages are delivered as before.
* ``llm``: the supervisor uses the team's main model (address, model and API key
  variable of the main model configuration).
* ``jev``: the supervisor uses the shared Jev connection of Models > Decision
  models (``browser.decision``, see ``resolve_jev_connection``).

No secret is stored: the spec carries only the name of the environment variable
that holds the key.

Kept free of ``jiuwenswarm.common.config`` so the Front process can import it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Callable

from jiuwenswarm.common.browser_settings import JEV_PROVIDER_DEFAULTS, resolve_jev_connection

TEAM_RULES_SECTION = "team_rules"
TEAM_RULES_MODES = ("off", "llm", "jev")
TEAM_RULES_MODE_PARAM = "team_rules_mode"
TEAM_RULES_PARAM_KEYS = frozenset({TEAM_RULES_MODE_PARAM})
JEV_KEY_MISSING = "Jev API key is required in Models > Jev settings"

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}$")


class TeamRulesSettingsError(ValueError):
    """A settings value the supervisor could not run with."""


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def team_rules_mode(config: Mapping[str, Any] | None) -> str:
    """The configured mode; anything unknown reads as ``off``."""
    raw = (config or {}).get(TEAM_RULES_SECTION)
    mode = _text(raw.get("mode")).lower() if isinstance(raw, dict) else ""
    return mode if mode in TEAM_RULES_MODES else "off"


def team_rules_panel_view(config: Mapping[str, Any] | None) -> dict[str, str]:
    """Flat config.get fields for the settings page."""
    return {TEAM_RULES_MODE_PARAM: team_rules_mode(config)}


def jev_key_env(config: Mapping[str, Any] | None) -> str:
    """Name of the variable holding the shared Jev key (``browser.decision``)."""
    browser = (config or {}).get("browser")
    decision = browser.get("decision") if isinstance(browser, dict) else None
    decision = decision if isinstance(decision, dict) else {}
    provider = decision.get("provider", "typesafe")
    if not isinstance(provider, str) or provider not in JEV_PROVIDER_DEFAULTS:
        provider = "typesafe"
    return _text(decision.get("api_key_env")) or JEV_PROVIDER_DEFAULTS[provider]["api_key_env"]


def build_team_rules_update(
    params: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate ``team_rules_mode`` in ``params``; return the section patch.

    Raises ``TeamRulesSettingsError`` before anything is written. An empty dict
    means the save does not touch the supervisor.
    """
    if TEAM_RULES_MODE_PARAM not in params:
        return {}
    mode = _text(params[TEAM_RULES_MODE_PARAM]).lower()
    if mode not in TEAM_RULES_MODES:
        raise TeamRulesSettingsError(f"invalid team_rules.mode: {mode!r}")
    if mode == "jev":
        environment = os.environ if environment is None else environment
        if not _text(environment.get(jev_key_env(config))):
            raise TeamRulesSettingsError(JEV_KEY_MISSING)
    return {"mode": mode}


def env_var_name(raw_value: Any) -> str:
    """``API_KEY`` for a ``${API_KEY}`` / ``${API_KEY:-...}`` reference, else ``""``."""
    match = _ENV_REF.match(_text(raw_value))
    return match.group(1) if match else ""


def _llm_spec(main_model: Mapping[str, Any] | None, key_env: str) -> dict[str, Any] | None:
    client = (main_model or {}).get("model_client_config")
    client = client if isinstance(client, Mapping) else {}
    api_base = _text(client.get("api_base")).rstrip("/")
    model = _text(client.get("model_name"))
    if not (api_base and model and key_env):
        return None
    # Anthropic models speak the Anthropic format. Every other provider (OpenAI and
    # the OpenAI-compatible ones: DeepSeek, SiliconFlow, DashScope, OpenRouter, relays)
    # is called in the OpenAI format; agent-core falls back to a choice tool when the
    # endpoint returns no logprobs.
    provider = _text(client.get("client_provider")).lower()
    return {
        "format": "anthropic" if provider == "anthropic" else "openai",
        "api_base": api_base,
        "model": model,
        "api_key_env": key_env,
    }


def _jev_spec(
    config: Mapping[str, Any], resolve_value: Callable[[Any], Any] | None,
) -> dict[str, Any]:
    connection = resolve_jev_connection(dict(config), resolve_value=resolve_value)
    return {
        "format": "jev",
        "api_base": connection["api_base"],
        "endpoint": connection["endpoint_path"],
        "model": connection["model_name"],
        "api_key_env": jev_key_env(config),
    }


def resolve_team_rules_spec(
    config: Mapping[str, Any] | None,
    *,
    main_model: Mapping[str, Any] | None = None,
    main_model_key_env: str = "",
    resolve_value: Callable[[Any], Any] | None = None,
) -> dict[str, Any] | None:
    """The ``TeamAgentSpec.team_rules`` dict, or ``None`` when the supervisor is off.

    ``main_model`` is the team's main model entry (``model_client_config`` with
    ``api_base`` / ``model_name`` / ``client_provider``) and ``main_model_key_env``
    the variable its API key comes from. Raises ``ValueError`` when the chosen
    connection is incomplete.
    """
    mode = team_rules_mode(config)
    if mode == "off":
        return None
    if mode == "jev":
        connection = _jev_spec(config or {}, resolve_value)
    else:
        connection = _llm_spec(main_model, main_model_key_env)
        if connection is None:
            raise ValueError(
                "team_rules mode llm needs the main model address, model name and an API key "
                "read from an environment variable"
            )
    return {"enabled": True, "mode": "active", **connection}
