"""Load SDK build inputs and resolve immutable installer-owned drivers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def engine_root() -> Path:
    """Return the SDK resources owned by this source tree or desktop bundle."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "computer_use" / "_resources"
    return Path(__file__).resolve().parent


def import_engine(root: Path):
    """Load the owned SDK by its exact path without changing import precedence."""
    source = (root / "src/computer_use/__init__.py").resolve()
    existing = sys.modules.get("computer_use")
    if existing is not None:
        origin = getattr(existing, "__file__", None)
        if origin is None or Path(origin).resolve() != source:
            raise RuntimeError("A computer_use package from a different location is already loaded.")
        return existing
    spec = importlib.util.spec_from_file_location("computer_use", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"The bundled Computer Use SDK is missing: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        for name in tuple(sys.modules):
            if name == spec.name or name.startswith(spec.name + "."):
                del sys.modules[name]
        raise
    return module


def prepare_native(source: Path | None = None) -> Path:
    """Resolve the installed runtime without compilation or cache extraction."""
    if source is None:
        if not getattr(sys, "frozen", False):
            raise RuntimeError("The bundled Computer Use backend is available only in desktop installers.")
        source = engine_root()
    from computer_use.paths import native_driver

    executable = Path(native_driver(root=source)["command"])
    files = [executable]
    if sys.platform == "win32":
        files.extend([executable.with_name("computer-use-capture.exe"),
                      executable.with_name("Interop.NativeUIA.dll")])
    if not all(path.is_file() for path in files):
        raise RuntimeError("The installed Computer Use driver is missing; reinstall the application.")
    return executable
