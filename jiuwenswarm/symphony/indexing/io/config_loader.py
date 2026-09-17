from __future__ import annotations

import json
from pathlib import Path

import yaml

from shared.storage import is_s3_uri, read_s3_text


def read_config_text(source: str | Path, *, description: str = "config") -> str:
    raw_source = str(source or "").strip()
    if not raw_source:
        raise ValueError(f"{description} path is empty")
    if is_s3_uri(raw_source):
        return read_s3_text(raw_source)
    path = Path(raw_source).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"{description} file not found: {path}")
    return path.read_text(encoding="utf-8")


def parse_json_or_yaml(text: str, *, source: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse config file: {source}") from exc


__all__ = ["parse_json_or_yaml", "read_config_text"]
