"""BM25 configuration; the agent's existing LLM makes the final choice."""
from dataclasses import asdict, dataclass
import json
import math


@dataclass(frozen=True)
class SelectionSettings:
    enabled: bool = False
    catalog_check_interval_s: float = 5.0
    candidate_k: int = 5
    text_max_chars: int = 2000
    max_query_chars: int = 1200
    query_timeout_s: float = 10.0
    startup_timeout_s: float = 60.0
    k1: float = 0.9
    b: float = 0.4
    name_weight: float = 0.5

    @classmethod
    def from_config(cls, config):
        flash = (config or {}).get('flash') or {}
        if not isinstance(flash, dict):
            raise ValueError('flash must be a mapping')
        section = flash.get('skill_selection') or {}
        if not isinstance(section, dict):
            raise ValueError('skill_selection must be a mapping')
        enabled = section.get('enabled', False)
        if not isinstance(enabled, bool):
            raise ValueError('skill_selection.enabled must be true or false')
        if not enabled:
            return cls()
        values = {'enabled': True}
        defaults = cls()
        for key, maximum in (('candidate_k', 5), ('text_max_chars', 4000), ('max_query_chars', 1200)):
            value = section.get(key, getattr(defaults, key))
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'skill_selection.{key} must be an integer in [1, {maximum}]')
            values[key] = value
        for key, low, high in (('query_timeout_s', 0.1, 600), ('startup_timeout_s', 0.1, 600),
                               ('catalog_check_interval_s', 0, 60),
                               ('k1', 0.01, 10), ('b', 0, 1), ('name_weight', 0, 2)):
            value = section.get(key, getattr(defaults, key))
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'skill_selection.{key} must be in [{low}, {high}]')
            values[key] = float(value)
        return cls(**values)

    def identity(self):
        return json.dumps(asdict(self), sort_keys=True)
