#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Runtime monkeypatches for openjiuwen logging behavior."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

_PATCH_APPLIED = False
_TOOL_LOG_FILE_DEFAULT = "run/tools.log"
_LOG_FILE_PATTERN_DEFAULT = "{name}-{pid}{ext}"
_PATCH_SOURCE_ROOT = Path(__file__).resolve().parent.parent / "openjiuwen_patch_sources"


class _OpenJiuwenPatchFinder(importlib.abc.MetaPathFinder):
    """Meta path finder that redirects selected openjiuwen modules to local patch sources."""

    def __init__(self, module_to_file: Dict[str, Path]) -> None:
        self._module_to_file = module_to_file

    @staticmethod
    def _package_search_locations(fullname: str, package_dir: Path) -> List[str]:
        locations: List[str] = [str(package_dir)]
        rel_parts = fullname.split(".")

        for base in sys.path:
            try:
                base_path = Path(base)
            except Exception:
                continue
            candidate_dir = base_path.joinpath(*rel_parts)
            candidate_init = candidate_dir / "__init__.py"
            if not candidate_init.exists():
                continue
            candidate_str = str(candidate_dir)
            if candidate_str not in locations:
                locations.append(candidate_str)
        return locations

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        file_path = self._module_to_file.get(fullname)
        if file_path is None:
            return None
        if not file_path.exists():
            return None

        if file_path.name == "__init__.py":
            search_locations = self._package_search_locations(fullname, file_path.parent)
            return importlib.util.spec_from_file_location(
                fullname,
                str(file_path),
                submodule_search_locations=search_locations,
            )

        return importlib.util.spec_from_file_location(fullname, str(file_path))


def _build_patch_module_map() -> Dict[str, Path]:
    module_to_file: Dict[str, Path] = {}
    root = _PATCH_SOURCE_ROOT
    if not root.exists():
        return module_to_file

    for py_file in root.rglob("*.py"):
        rel = py_file.relative_to(root)
        if not rel.parts or rel.parts[0] != "openjiuwen":
            continue
        if py_file.name == "__init__.py":
            module_name = ".".join(rel.parts[:-1])
        else:
            module_name = ".".join(rel.with_suffix("").parts)
        if module_name:
            module_to_file[module_name] = py_file
    return module_to_file


def _install_openjiuwen_import_hook() -> None:
    module_to_file = _build_patch_module_map()
    if not module_to_file:
        return

    for finder in sys.meta_path:
        if isinstance(finder, _OpenJiuwenPatchFinder):
            finder._module_to_file.update(module_to_file)
            return

    sys.meta_path.insert(0, _OpenJiuwenPatchFinder(module_to_file))


def _patch_default_inner_config(constant_mod: Any, log_config_mod: Any) -> None:
    shared_log_file = (os.getenv("PLAYWRIGHT_RUNTIME_SHARED_LOG_FILE") or "").strip().lower()
    use_pid_log_pattern = shared_log_file not in {"1", "true", "yes", "on"}

    constant_mod.DEFAULT_INNER_LOG_CONFIG.setdefault("tool_log_file", _TOOL_LOG_FILE_DEFAULT)
    if use_pid_log_pattern and not constant_mod.DEFAULT_INNER_LOG_CONFIG.get("log_file_pattern"):
        constant_mod.DEFAULT_INNER_LOG_CONFIG["log_file_pattern"] = _LOG_FILE_PATTERN_DEFAULT

    module_default = getattr(log_config_mod, "DEFAULT_INNER_LOG_CONFIG", None)
    if isinstance(module_default, dict):
        module_default.setdefault("tool_log_file", _TOOL_LOG_FILE_DEFAULT)
        if use_pid_log_pattern and not module_default.get("log_file_pattern"):
            module_default["log_file_pattern"] = _LOG_FILE_PATTERN_DEFAULT

    log_config = getattr(log_config_mod, "log_config", None)
    runtime_log_config = getattr(log_config, "_log_config", None)
    if isinstance(runtime_log_config, dict):
        runtime_log_config.setdefault("tool_log_file", _TOOL_LOG_FILE_DEFAULT)
        if use_pid_log_pattern and not runtime_log_config.get("log_file_pattern"):
            runtime_log_config["log_file_pattern"] = _LOG_FILE_PATTERN_DEFAULT


def _patch_log_config(log_config_mod: Any) -> None:
    if getattr(log_config_mod.LogConfig, "_browser_move_tool_patch", False):
        return

    original_load_config = log_config_mod.LogConfig._load_config
    original_get_common_config = log_config_mod.LogConfig.get_common_config

    def _load_config_with_tool_log(config_path: str) -> Dict[str, Any]:
        config = original_load_config(config_path)
        if isinstance(config, dict):
            config.setdefault("tool_log_file", _TOOL_LOG_FILE_DEFAULT)
            shared_log_file = (os.getenv("PLAYWRIGHT_RUNTIME_SHARED_LOG_FILE") or "").strip().lower()
            use_pid_log_pattern = shared_log_file not in {"1", "true", "yes", "on"}
            if use_pid_log_pattern and not config.get("log_file_pattern"):
                config["log_file_pattern"] = _LOG_FILE_PATTERN_DEFAULT
        return config

    def _get_common_config_with_tool_log(self: Any) -> Dict[str, Any]:
        common_config = original_get_common_config(self)

        tool_log_file = self._log_config.get("tool_log_file", _TOOL_LOG_FILE_DEFAULT)
        if tool_log_file:
            full_tool_log_file = os.path.join(self._log_path, tool_log_file)
            log_config_mod.normalize_and_validate_log_path(full_tool_log_file)
            common_config["tool_log_file"] = full_tool_log_file

        return common_config

    log_config_mod.LogConfig._load_config = staticmethod(_load_config_with_tool_log)
    log_config_mod.LogConfig.get_common_config = _get_common_config_with_tool_log
    log_config_mod.LogConfig._browser_move_tool_patch = True


def _install_tool_file_handler(default_impl_mod: Any, tool_only_filter_cls: type, logger_obj: Any) -> None:
    output = logger_obj.config.get("output", ["console"])
    if "file" not in output:
        return

    tool_log_file = logger_obj.config.get("tool_log_file")
    if not tool_log_file:
        return

    default_impl_mod.normalize_and_validate_log_path(tool_log_file)
    try:
        abs_tool_log_file = os.path.abspath(os.path.expanduser(tool_log_file))
    except (OSError, TypeError):
        abs_tool_log_file = tool_log_file

    target_file = os.path.normcase(os.path.abspath(abs_tool_log_file))

    for handler in logger_obj._logger.handlers:
        base_filename = getattr(handler, "baseFilename", None)
        if not base_filename:
            continue
        if os.path.normcase(os.path.abspath(base_filename)) != target_file:
            continue
        for current_filter in getattr(handler, "filters", []):
            if isinstance(current_filter, tool_only_filter_cls):
                return

    tool_log_dir = os.path.dirname(abs_tool_log_file)
    if tool_log_dir:
        try:
            os.makedirs(tool_log_dir, mode=0o750, exist_ok=True)
        except OSError as error:
            raise default_impl_mod.build_error(
                default_impl_mod.StatusCode.COMMON_LOG_PATH_INIT_FAILED,
                error_msg=f"the log_dir is `{tool_log_dir}`, error detail: {error}",
            ) from error

    backup_count = logger_obj.config.get("backup_count", 20)
    max_bytes = default_impl_mod.get_log_max_bytes(logger_obj.config.get("max_bytes", 20 * 1024 * 1024))
    log_file_pattern = logger_obj.config.get("log_file_pattern", None)
    backup_file_pattern = logger_obj.config.get("backup_file_pattern", None)

    tool_file_handler = default_impl_mod.SafeRotatingFileHandler(
        filename=abs_tool_log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        log_file_pattern=log_file_pattern,
        backup_file_pattern=backup_file_pattern,
    )
    tool_file_handler.addFilter(default_impl_mod.ContextFilter(logger_obj.log_type))
    tool_file_handler.addFilter(tool_only_filter_cls())
    tool_file_handler.setFormatter(logger_obj._get_formatter())
    logger_obj._logger.addHandler(tool_file_handler)


def _patch_default_logger(default_impl_mod: Any) -> None:
    if not hasattr(default_impl_mod, "ToolOnlyFilter"):

        class ToolOnlyFilter(logging.Filter):
            """Keep only tool invocation lifecycle records."""

            _TOOL_EVENT_MARKERS = (
                '"event_type": "tool_call_start"',
                '"event_type": "tool_call_end"',
                '"event_type": "tool_call_error"',
                '"module_type": "tool"',
            )

            def filter(self, record: logging.LogRecord) -> bool:
                record_log_type = getattr(record, "log_type", "")
                if record_log_type == "tool":
                    return True

                message = record.getMessage()
                if any(marker in message for marker in self._TOOL_EVENT_MARKERS):
                    return True

                lowered = message.lower()
                if "executing tool:" in lowered:
                    return True
                if "calling tool '" in lowered:
                    return True
                if "tool '" in lowered and "call completed" in lowered:
                    return True

                return False

        default_impl_mod.ToolOnlyFilter = ToolOnlyFilter

    if getattr(default_impl_mod.DefaultLogger, "_browser_move_tool_patch", False):
        return

    original_setup_logger = default_impl_mod.DefaultLogger._setup_logger
    tool_only_filter_cls = default_impl_mod.ToolOnlyFilter

    def _setup_logger_with_tool_handler(self: Any) -> None:
        original_setup_logger(self)
        _install_tool_file_handler(default_impl_mod, tool_only_filter_cls, self)

    default_impl_mod.DefaultLogger._setup_logger = _setup_logger_with_tool_handler
    default_impl_mod.DefaultLogger._browser_move_tool_patch = True


def _refresh_common_logger(log_config_mod: Any, log_manager_cls: Any) -> None:
    if not getattr(log_manager_cls, "_initialized", False):
        return

    common_logger = getattr(log_manager_cls, "_loggers", {}).get("common")
    if common_logger is None or not hasattr(common_logger, "reconfigure"):
        return

    common_logger.reconfigure(log_config_mod.log_config.get_common_config())


def _patch_llm_provider_aliases(model_mod: Any) -> None:
    """Restore provider aliases that existed in the vendored openjiuwen copy."""
    registry = getattr(model_mod, "_CLIENT_TYPE_REGISTRY", None)
    if not isinstance(registry, dict):
        return

    openai_client = registry.get("OpenAI")
    if openai_client is None:
        return

    registry.setdefault("OpenRouter", openai_client)
    registry.setdefault("openrouter", openai_client)
    registry.setdefault("openai", openai_client)


def _tool_param_score(params: Any) -> int:
    """Heuristic score for tool parameter richness (higher is better)."""
    if params is None:
        return 0
    if isinstance(params, dict):
        score = 1
        properties = params.get("properties")
        if isinstance(properties, dict):
            score += len(properties) * 10
        required = params.get("required")
        if isinstance(required, list):
            score += len(required) * 3
        if "$schema" in params:
            score += 5
        if params:
            score += 1
        return score
    if hasattr(params, "model_dump"):
        try:
            return _tool_param_score(params.model_dump())
        except Exception:
            return 1
    return 1


def _dedupe_tool_info_list(tool_infos: Iterable[Any]) -> List[Any]:
    """Deduplicate tool infos by name, preferring richer parameter schemas."""
    ordered_names: List[str] = []
    best_by_name: Dict[str, Any] = {}
    best_score: Dict[str, int] = {}

    for tool in tool_infos or []:
        name = (getattr(tool, "name", None) or "").strip()
        if not name:
            continue

        params = getattr(tool, "parameters", None)
        score = _tool_param_score(params)
        if name not in best_by_name:
            ordered_names.append(name)
            best_by_name[name] = tool
            best_score[name] = score
            continue

        if score >= best_score[name]:
            best_by_name[name] = tool
            best_score[name] = score

    return [best_by_name[name] for name in ordered_names if name in best_by_name]


def _patch_ability_manager(ability_manager_mod: Any) -> None:
    """Patch AbilityManager.list_tool_info to avoid duplicate tool names."""
    ability_cls = getattr(ability_manager_mod, "AbilityManager", None)
    if ability_cls is None:
        return
    if getattr(ability_cls, "_browser_move_tool_dedupe_patch", False):
        return

    original_list_tool_info = ability_cls.list_tool_info

    async def _list_tool_info_dedup(self: Any, *args: Any, **kwargs: Any) -> List[Any]:
        result = await original_list_tool_info(self, *args, **kwargs)
        return _dedupe_tool_info_list(result)

    ability_cls.list_tool_info = _list_tool_info_dedup
    ability_cls._browser_move_tool_dedupe_patch = True


def _patch_base_model_client(base_model_client_mod: Any) -> None:
    """Patch model tool serialization to enforce unique tool names."""
    base_cls = getattr(base_model_client_mod, "BaseModelClient", None)
    if base_cls is None:
        return
    if getattr(base_cls, "_browser_move_tool_dedupe_patch", False):
        return

    original_convert_tools = base_cls._convert_tools_to_dict

    try:
        sig = inspect.signature(original_convert_tools)
        param_count = len(sig.parameters)
        needs_self = param_count == 2 and "self" in sig.parameters
    except Exception:
        # Fallback: try with self first
        needs_self = True

    def _convert_tools_to_dict_dedup(self: Any, tools: Any) -> Any:
        if needs_self:
            tool_dicts = original_convert_tools(self, tools)
        else:
            tool_dicts = original_convert_tools(tools)

        if not tool_dicts:
            return tool_dicts

        ordered_names: List[str] = []
        best_by_name: Dict[str, Dict[str, Any]] = {}
        best_score: Dict[str, int] = {}
        for tool_dict in tool_dicts:
            function = tool_dict.get("function", {}) if isinstance(tool_dict, dict) else {}
            name = str(function.get("name", "")).strip()
            if not name:
                continue
            score = _tool_param_score(function.get("parameters"))
            if name not in best_by_name:
                ordered_names.append(name)
                best_by_name[name] = tool_dict
                best_score[name] = score
                continue
            if score >= best_score[name]:
                best_by_name[name] = tool_dict
                best_score[name] = score

        return [best_by_name[name] for name in ordered_names if name in best_by_name]

    base_cls._convert_tools_to_dict = _convert_tools_to_dict_dedup
    base_cls._browser_move_tool_dedupe_patch = True


def apply_openjiuwen_monkeypatch() -> None:
    """Apply runtime monkeypatches for openjiuwen logging."""
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    _install_openjiuwen_import_hook()

    try:
        constant_mod = importlib.import_module("openjiuwen.core.common.logging.default.constant")
        default_impl_mod = importlib.import_module("openjiuwen.core.common.logging.default.default_impl")
        log_config_mod = importlib.import_module("openjiuwen.core.common.logging.default.log_config")
        manager_mod = importlib.import_module("openjiuwen.core.common.logging.manager")
        model_mod = importlib.import_module("openjiuwen.core.foundation.llm.model")
        ability_manager_mod = importlib.import_module("openjiuwen.core.single_agent.ability_manager")
        base_model_client_mod = importlib.import_module("openjiuwen.core.foundation.llm.model_clients.base_model_client")
    except Exception:
        return

    _patch_default_inner_config(constant_mod, log_config_mod)
    _patch_log_config(log_config_mod)
    _patch_default_logger(default_impl_mod)
    _refresh_common_logger(log_config_mod, manager_mod.LogManager)
    _patch_llm_provider_aliases(model_mod)
    _patch_ability_manager(ability_manager_mod)
    _patch_base_model_client(base_model_client_mod)
    _PATCH_APPLIED = True
