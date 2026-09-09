# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight display metadata for the process-style CLI shell."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from jiuwenswarm.common.mode_matrix import (
    TEAM_PLAN_CODE_MODE,
    TEAM_PLAN_NORMAL_MODE,
    canonicalize_mode_text,
    resolve_request_mode,
)

_ENV_PATTERN = re.compile(r"\$\{([^:}]+)(?::-([^}]*))?\}")
_UNCONFIGURED_MODEL = "未配置"


def _config_file_candidates() -> list[Path]:
    candidates: list[Path] = []
    explicit_config_dir = str(os.getenv("JIUWENSWARM_CONFIG_DIR") or "").strip()
    if explicit_config_dir:
        explicit = Path(explicit_config_dir).expanduser()
        candidates.append(
            explicit
            if explicit.name.endswith((".yaml", ".yml"))
            else explicit / "config.yaml"
        )
    else:
        data_dir = str(os.getenv("JIUWENSWARM_DATA_DIR") or "").strip()
        if data_dir:
            candidates.append(Path(data_dir).expanduser() / "config" / "config.yaml")
        else:
            user_home = Path(
                str(os.getenv("JIUWENSWARM_HOME") or "").strip() or Path.home()
            ).expanduser()
            candidates.append(user_home / ".jiuwenswarm" / "config" / "config.yaml")

    package_root = Path(__file__).resolve().parents[2]
    candidates.append(package_root / "resources" / "config.yaml")

    result: list[Path] = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def _dotenv_values(config_file: Path) -> dict[str, str | None]:
    env_file = config_file.parent / ".env"
    if not env_file.is_file():
        return {}
    try:
        from dotenv import dotenv_values

        return {
            str(key): value
            for key, value in dotenv_values(env_file).items()
            if key is not None
        }
    except Exception:  # noqa: BLE001 - display metadata is best effort
        return {}


def _resolve_env_text(value: object, dotenv: Mapping[str, str | None]) -> str:
    if not isinstance(value, str):
        return ""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        current = dotenv.get(name) if name in dotenv else os.getenv(name)
        if default is not None and (current is None or current == ""):
            return default
        return str(current or "")

    return _ENV_PATTERN.sub(replace, value).strip()


def select_configured_model_name(
    entries: Any,
    *,
    dotenv: Mapping[str, str | None] | None = None,
) -> str | None:
    """Select a model name without retaining or exposing model credentials."""
    if not isinstance(entries, list):
        return None

    environment = dotenv or {}
    # This is intentionally a lightweight UI preview: it does not construct
    # model clients or validate credentials. Runtime may therefore select a
    # later usable entry when the first configured candidate cannot be built.
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_client_config = entry.get("model_client_config")
        if not isinstance(model_client_config, dict):
            continue
        model_name = _resolve_env_text(
            model_client_config.get("model_name"),
            environment,
        )
        if model_name:
            return model_name
    return None


def _model_entries(config: Mapping[str, Any]) -> list[Any]:
    models = config.get("models")
    if not isinstance(models, dict):
        return []
    defaults = models.get("defaults")
    if isinstance(defaults, list) and defaults:
        return defaults
    default = models.get("default")
    return [default] if isinstance(default, dict) else []


def _legacy_model_name(
    config: Mapping[str, Any],
    dotenv: Mapping[str, str | None],
) -> str:
    react = config.get("react")
    if not isinstance(react, dict):
        return ""
    model_client_config = react.get("model_client_config")
    if isinstance(model_client_config, dict):
        model_name = _resolve_env_text(
            model_client_config.get("model_name"),
            dotenv,
        )
        if model_name:
            return model_name
    return _resolve_env_text(react.get("model_name"), dotenv)


def resolve_configured_model_name() -> str:
    """Infer the next worker's configured model without starting Runtime."""
    shell_fallback = str(os.getenv("MODEL_NAME") or "").strip()
    for config_file in _config_file_candidates():
        if not config_file.is_file():
            continue
        dotenv = _dotenv_values(config_file)
        dotenv_fallback = str(dotenv.get("MODEL_NAME") or "").strip()
        try:
            loaded = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError, UnicodeError):
            return dotenv_fallback or shell_fallback or _UNCONFIGURED_MODEL
        if not isinstance(loaded, dict):
            return dotenv_fallback or shell_fallback or _UNCONFIGURED_MODEL

        selected = select_configured_model_name(
            _model_entries(loaded),
            dotenv=dotenv,
        )
        if selected:
            return selected
        legacy = _legacy_model_name(loaded, dotenv)
        return legacy or dotenv_fallback or shell_fallback or _UNCONFIGURED_MODEL
    return shell_fallback or _UNCONFIGURED_MODEL


def _resolve_cli_mode(
    raw_mode: Any,
    *,
    work_mode: Any = None,
) -> tuple[str, str | None, str]:
    """Mirror Runtime's transport-free legacy mode normalization for display."""
    mode_text = canonicalize_mode_text(raw_mode)
    normalized_work_mode = (
        work_mode.strip().lower() if isinstance(work_mode, str) else ""
    )

    if mode_text in ("plan", "fast"):
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        return "agent", None, "agent"
    if mode_text == TEAM_PLAN_NORMAL_MODE:
        return "team", "plan", TEAM_PLAN_NORMAL_MODE
    if mode_text == TEAM_PLAN_CODE_MODE:
        return "code", "team", TEAM_PLAN_CODE_MODE

    parts = mode_text.split(".")
    manager_mode = parts[0] or "agent"
    if manager_mode == "agent":
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        return "agent", None, "agent"
    if manager_mode == "team":
        return "team", None, "team"

    sub_mode = parts[1] if len(parts) > 1 and parts[1] else None
    if manager_mode == "code" and sub_mode not in {"plan", "normal", "team"}:
        sub_mode = "normal"
    if manager_mode == "code" and sub_mode is None:
        sub_mode = "normal"
    canonical_mode = f"{manager_mode}.{sub_mode}" if sub_mode else manager_mode
    if canonical_mode in {"agent", "code", "code.normal"}:
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        if normalized_work_mode == "work":
            return "agent", None, "agent"
    return manager_mode, sub_mode, canonical_mode


def resolve_display_mode(mode: str, work_mode: str) -> str:
    """Collapse mode/work_mode into the canonical mode shown by the UI."""
    resolved = resolve_request_mode(
        {"mode": mode, "work_mode": work_mode},
        _resolve_cli_mode,
        work_mode=work_mode,
    )
    return resolved.canonical_mode


__all__ = [
    "resolve_configured_model_name",
    "resolve_display_mode",
    "select_configured_model_name",
]
