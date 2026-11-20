from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _load_simple_yaml(config_path: str) -> dict:
    """
    Small YAML subset parser used when PyYAML is unavailable.

    The skill config intentionally uses only:
    - top-level sections
    - section -> key -> list[str]
    - section -> list[str]
    - section -> key -> scalar
    """
    config: dict = {}
    current_section: Optional[str] = None
    current_key: Optional[str] = None

    with open(config_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue

            indent = len(line) - len(line.lstrip(" "))
            text = line.strip()

            if indent == 0 and text.endswith(":"):
                current_section = text[:-1].strip()
                current_key = None
                config[current_section] = {}
                continue

            if current_section is None:
                continue

            if indent == 2 and text.startswith("- "):
                if not isinstance(config.get(current_section), list):
                    config[current_section] = []
                config[current_section].append(_strip_quotes(text[2:]))
                continue

            if indent == 2 and ":" in text:
                key, value = text.split(":", 1)
                current_key = key.strip()
                value = value.strip()
                if value:
                    config[current_section][current_key] = _strip_quotes(value)
                else:
                    config[current_section][current_key] = []
                continue

            if indent == 4 and text.startswith("- ") and current_key is not None:
                values = config[current_section].setdefault(current_key, [])
                if isinstance(values, list):
                    values.append(_strip_quotes(text[2:]))

    return config


def _load_config(config_path: str) -> dict:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        return _load_simple_yaml(config_path)

    with open(config_path, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return loaded or {}


class PhaseMapper:
    def __init__(self, config_path: str):
        config = _load_config(config_path)

        self.phase_patterns: Dict[str, List[str]] = config.get("phases", {})
        self.focus_phases: List[str] = config.get("focus_phases", [])
        self.phase_categories: Dict[str, str] = config.get("phase_categories", {})

        self._compiled: List[Tuple[str, str, re.Pattern]] = []
        for phase, patterns in self.phase_patterns.items():
            for pattern in patterns:
                self._compiled.append((phase, pattern, re.compile(pattern)))

    def map_event_name(self, event_name: str) -> Optional[str]:
        """
        选择最具体的匹配：
        - 所有命中的规则里，优先选择 pattern 字符串更长的
        - 若长度相同，保留先出现的
        """
        best_phase = None
        best_pattern_len = -1

        for phase, pattern_str, pattern in self._compiled:
            if pattern.search(event_name):
                score = len(pattern_str)
                if score > best_pattern_len:
                    best_phase = phase
                    best_pattern_len = score

        return best_phase
