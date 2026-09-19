"""The Co-scribe watcher, on the gateway side."""

from .cursor_store import CloudDocStore, get_clouddoc_state_path

__all__ = ["CloudDocStore", "get_clouddoc_state_path"]
