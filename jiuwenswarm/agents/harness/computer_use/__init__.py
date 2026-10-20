"""CUA subagent integration for native tools and the programmatic engine."""
from pathlib import Path


def engine_root() -> Path:
    return Path(__file__).resolve().parents[3] / "cua_engine"
