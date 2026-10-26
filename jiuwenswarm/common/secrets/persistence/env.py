"""Env medium adapter (.env single variable)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from jiuwenswarm.common.local_env_config import set_os_environ, update_process_baseline
from jiuwenswarm.common.secrets.registry import StorageLocation

logger = logging.getLogger(__name__)


class EnvMediumAdapter:
    def __init__(self, env_path: Path) -> None:
        self._env_path = env_path

    def read_raw(self, loc: StorageLocation) -> str:
        if loc.medium != "env":
            raise ValueError("EnvMediumAdapter requires medium=env")
        return _read_env_var(self._env_path, loc.path)

    def write_raw(self, loc: StorageLocation, raw: str) -> None:
        if loc.medium != "env":
            raise ValueError("EnvMediumAdapter requires medium=env")
        name = loc.path
        if raw == "":
            set_os_environ(name, None)
            update_process_baseline({name: None})
            _persist_env_updates(self._env_path, {name: ""})
            return
        set_os_environ(name, raw)
        update_process_baseline({name: raw})
        _persist_env_updates(self._env_path, {name: raw})

    def delete_raw(self, loc: StorageLocation) -> None:
        self.write_raw(loc, "")


def _read_env_var(env_path: Path, name: str) -> str:
    if not env_path.is_file():
        return ""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(rf"^{re.escape(name)}=(.*)$", stripped)
            if not m:
                continue
            return _unquote_env_value(m.group(1).strip())
    except OSError as exc:
        logger.warning("Failed to read .env %s: %s", env_path, exc)
    return ""


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        inner = value[1:-1]
        return inner.replace(f"\\{value[0]}", value[0])
    return value


def _persist_env_updates(env_path: Path, updates: dict[str, str]) -> None:
    if not updates:
        return
    try:
        lines: list[str] = []
        if env_path.is_file():
            lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            found = False
            for env_key, value in updates.items():
                if stripped.startswith(env_key + "="):
                    new_lines.append(f'{env_key}="{value}"\n' if value else f"{env_key}=\n")
                    found = True
                    break
            if not found:
                new_lines.append(line if line.endswith("\n") else line + "\n")
        for env_key, value in updates.items():
            if not any(s.strip().startswith(env_key + "=") for s in new_lines):
                new_lines.append(f'{env_key}="{value}"\n' if value else f"{env_key}=\n")
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("".join(new_lines), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to persist .env %s: %s", env_path, exc)
