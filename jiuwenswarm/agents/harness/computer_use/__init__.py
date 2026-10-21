"""CUA subagent integration for native tools and the programmatic engine."""
from pathlib import Path
import sys

__all__ = ["engine_root", "import_engine"]


def import_engine(root: Path):
    from jiuwenswarm.cua_engine.hosting import import_engine as load

    return load(root)


def engine_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "computer_use" / "_resources"
    return Path(__file__).resolve().parents[3] / "cua_engine"
