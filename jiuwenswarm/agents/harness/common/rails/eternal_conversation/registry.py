"""Process-level Session ownership for eternal-conversation coordinators."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from weakref import WeakValueDictionary

from .coordinator import SessionCoordinator


_COORDINATORS: WeakValueDictionary[tuple[str, str], SessionCoordinator] = (
    WeakValueDictionary()
)


def get_session_coordinator(
    root: Path,
    session_id: str,
    model_supplier: Callable[[], Any],
) -> SessionCoordinator:
    """Return the one coordinator for a durable product Session.

    Web/TUI may retire and recreate their session-scoped Adapter while the two
    background Agents are still running.  Coordinator ownership therefore
    follows the product Session, not the short-lived Adapter/Rail instance.
    """
    key = (str(root.resolve()), session_id)
    coordinator = _COORDINATORS.get(key)
    if coordinator is None:
        coordinator = SessionCoordinator(root, session_id, model_supplier)
        _COORDINATORS[key] = coordinator
    else:
        coordinator.agents.set_model_supplier(model_supplier)
    return coordinator


async def close_all_session_coordinators() -> None:
    """Explicit process-shutdown/test hook; ordinary Rail cleanup must not call it."""
    coordinators = list(_COORDINATORS.values())
    _COORDINATORS.clear()
    for coordinator in coordinators:
        await coordinator.close()


__all__ = ["close_all_session_coordinators", "get_session_coordinator"]
