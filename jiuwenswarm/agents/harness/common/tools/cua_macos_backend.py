"""Compatibility imports for the embedded macOS desktop adapter."""

from jiuwenswarm.cua_engine.macos_backend import (
    MacOSBackend,
    MacOSLaunchTool,
    install_macos_backend,
    normalize_key,
)

__all__ = ["MacOSBackend", "MacOSLaunchTool", "install_macos_backend", "normalize_key"]
