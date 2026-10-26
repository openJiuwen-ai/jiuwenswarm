# -*- coding: utf-8 -*-
"""Small shared utilities for fingerprint normalization."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict


def normalize_token(raw: str) -> str:
    value = unicodedata.normalize("NFKC", raw)
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value


def normalize_parameter_name(raw: str) -> str:
    return normalize_token(raw) or "input"


def normalize_slug(raw: str) -> str:
    value = unicodedata.normalize("NFKC", raw)
    value = value.strip().lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def normalize_human_name(raw: str) -> str:
    return str(raw).strip()


def to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}