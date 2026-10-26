"""Enterprise edition gateway extension bundles (filesystem roots for ExtensionManager)."""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"

_ROOT = Path(__file__).resolve().parent


def extensions_root() -> Path:
    """九问网关侧外置扩展目录（含 agent_client、runtime_management 等）。"""
    return _ROOT / "extensions"