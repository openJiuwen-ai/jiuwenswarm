"""Symphony-specific agent rails and stream lifecycle helpers."""

from importlib import import_module

# Stream-only consumers do not need either rail or the skill retrieval toolkit.
_EXPORTS = {
    "SymphonyOrchestrationRail": ".orchestration_rail",
    "SymphonyRetrievalRail": ".retrieval_rail",
    "SymphonyToolStreamHandler": ".tool_stream_events",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> type:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
