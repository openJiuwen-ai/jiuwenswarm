"""Resolve IM CLI binaries (PATH + common install locations). Not a product setting."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_CHANNEL_BINARIES: dict[str, tuple[str, ...]] = {
    "feishu": ("lark-cli", "lark-cli.cmd"),
    "dingtalk": ("dws", "dws.cmd"),
    "welink": ("welink-cli", "welink-cli.cmd"),
}


def resolve_cli_path(channel_id: str) -> str | None:
    names = _CHANNEL_BINARIES.get((channel_id or "").strip())
    if not names:
        return None
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for candidate in _windows_npm_candidates(names):
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_npm_candidates(names: tuple[str, ...]) -> list[Path]:
    if os.name != "nt":
        return []
    appdata = os.environ.get("APPDATA") or ""
    roaming_npm = Path(appdata) / "npm" if appdata else None
    local = os.environ.get("LOCALAPPDATA") or ""
    out: list[Path] = []
    for name in names:
        if roaming_npm is not None:
            out.append(roaming_npm / name)
        if local:
            out.append(Path(local) / "npm" / name)
    return out
