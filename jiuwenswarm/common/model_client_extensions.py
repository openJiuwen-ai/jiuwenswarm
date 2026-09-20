"""Load selected external model-client source files at process startup."""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path
from types import ModuleType


logger = logging.getLogger(__name__)

_ENV_NAME = "AGENT_EXTRA_MODEL_CLIENTS"
_LOADED_MODULES: dict[str, ModuleType] = {}
_LOADING_MODULES: set[str] = set()
_LOAD_LOCK = threading.RLock()


def _selected_module_names() -> list[str]:
    """Return valid, de-duplicated Python file basenames from the env value."""
    names: list[str] = []
    seen: set[str] = set()
    for value in os.getenv(_ENV_NAME, "").split(","):
        name = value.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if not name.isidentifier():
            logger.error(
                "[ModelClientExtensions] Ignore invalid module name %r in %s",
                name,
                _ENV_NAME,
            )
            continue
        names.append(name)
    return names


def _extension_roots() -> list[Path]:
    """Resolve configured extension roots, followed by the built-in root."""
    values = [value.strip() for value in os.getenv("EXTENSION_DIRS", "").split(";")]
    roots = [Path(value).expanduser().resolve() for value in values if value]
    roots.append(Path(__file__).resolve().parents[1] / "extensions")

    result: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _find_module_file(name: str, roots: list[Path]) -> Path | None:
    for root in roots:
        candidate = root / "model_clients" / f"{name}.py"
        if candidate.is_file():
            return candidate.resolve()
    return None


def _rollback_module_clients(import_name: str, previous_clients: set[str]) -> None:
    from openjiuwen.core.common.clients import get_client_registry

    registry = get_client_registry()
    registered_classes = getattr(registry, "_client_classes", {})
    for full_name in set(registry.list_clients()) - previous_clients:
        client_class = registered_classes.get(full_name)
        if getattr(client_class, "__module__", None) == import_name:
            registry.unregister(full_name)


def _import_module(name: str, source: Path) -> ModuleType:
    from openjiuwen.core.common.clients import get_client_registry

    path_digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
    import_name = f"_jiuwenswarm_model_client_{name}_{path_digest}"
    spec = importlib.util.spec_from_file_location(import_name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create import spec for {source}")

    module = importlib.util.module_from_spec(spec)
    previous_clients = set(get_client_registry().list_clients())
    sys.modules[import_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _rollback_module_clients(import_name, previous_clients)
        sys.modules.pop(import_name, None)
        raise
    return module


def load_extra_model_clients() -> list[ModuleType]:
    """Import model-client files selected by ``AGENT_EXTRA_MODEL_CLIENTS``."""
    names = _selected_module_names()
    if not names:
        return []

    roots = _extension_roots()
    for name in names:
        with _LOAD_LOCK:
            if name in _LOADED_MODULES:
                continue
            if name in _LOADING_MODULES:
                logger.warning(
                    "[ModelClientExtensions] Skip re-entrant load of %s",
                    name,
                )
                continue
            source = _find_module_file(name, roots)
            if source is None:
                logger.error(
                    "[ModelClientExtensions] %s.py not found under model_clients in %s",
                    name,
                    [str(root) for root in roots],
                )
                continue
            _LOADING_MODULES.add(name)

        try:
            module = _import_module(name, source)
        except Exception:  # noqa: BLE001 - isolate customer extension failures
            logger.exception(
                "[ModelClientExtensions] Failed to load %s from %s",
                name,
                source,
            )
        else:
            with _LOAD_LOCK:
                _LOADED_MODULES[name] = module
            logger.info(
                "[ModelClientExtensions] Loaded %s from %s",
                name,
                source,
            )
        finally:
            with _LOAD_LOCK:
                _LOADING_MODULES.discard(name)

    with _LOAD_LOCK:
        return [_LOADED_MODULES[name] for name in names if name in _LOADED_MODULES]


__all__ = ["load_extra_model_clients"]
