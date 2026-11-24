"""Session retrieval, graph orchestration, and SkillPack tools."""

from importlib import import_module

_EXPORTS = {
    "SkillPackToolkit": ".skillpack_toolkit",
    "SkillRetrievalToolkit": ".retrieval_toolkit",
    "SymphonyToolkit": ".orchestration_toolkit",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> type:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
