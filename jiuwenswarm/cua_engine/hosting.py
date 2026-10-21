"""Embedding helpers for SDK loading and immutable native runtime preparation.

The host supplies its resource and cache paths; no Agent framework is imported.
"""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile

import portalocker


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


def prepare_bundle(source: Path, cache: Path) -> Path:
    """Copy only runtime sources; never compile into site-packages."""
    roots = ("src", "backends/macos/Sources", "backends/windows", "assets", "skills")
    files = [source / "pyproject.toml", source / "backends/macos/Package.swift"]
    for name in roots:
        for path in (source / name).rglob("*"):
            parts = path.relative_to(source).parts
            if path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in parts):
                files.append(path)
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / digest.hexdigest()[:24]
    with portalocker.Lock(str(target) + ".lock", timeout=180):
        if not target.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".prepare-", dir=cache))
            try:
                for path in files:
                    destination = temporary / path.relative_to(source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                temporary.rename(target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
    return target


def _runtime_ready(executable: Path) -> bool:
    files = [executable]
    if sys.platform == "win32":
        files.extend([executable.with_name("computer-use-capture.exe"),
                      executable.with_name("Interop.NativeUIA.dll")])
    return all(path.is_file() for path in files)


def _build_native(root: Path, build_log: Path | None) -> None:
    from computer_use.scripts.native import build_native

    with ExitStack() as resources:
        output = None
        if build_log is not None:
            build_log.parent.mkdir(parents=True, exist_ok=True)
            output = resources.enter_context(build_log.open("a", encoding="utf-8"))
        destination = output if output is not None else sys.stderr
        resources.enter_context(redirect_stdout(destination))
        resources.enter_context(redirect_stderr(destination))
        descriptors = [(number, os.dup(number)) for number in (1, 2)]
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            if output is not None:
                os.dup2(output.fileno(), 2)
            # Compiler output must never become an MCP JSON-RPC response.
            os.dup2(2, 1)
            build_native(root=root)
        except Exception as error:
            if output is not None:
                output.write(f"\n{type(error).__name__}: {error}\n")
            raise
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            for number, saved in descriptors:
                os.dup2(saved, number)
                os.close(saved)


def prepare_native(source: Path, *, cache: Path | None = None, build_log: Path | None = None) -> Path:
    """Use a prebuilt runtime, or materialize and build in an explicit cache."""
    root = prepare_bundle(source, cache) if cache is not None else source
    if cache is not None:
        import_engine(root)
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root / "src"), os.environ.get("PYTHONPATH")]))
    from computer_use.paths import native_driver

    executable = Path(native_driver(root=root)["command"])
    if not _runtime_ready(executable):
        if cache is None:
            raise RuntimeError("The installed Computer Use driver is missing; reinstall the application.")
        with portalocker.Lock(str(root) + ".build.lock", timeout=180):
            if not _runtime_ready(executable):
                try:
                    _build_native(root, build_log)
                except Exception as error:
                    detail = f" Build log: {build_log}" if build_log is not None else ""
                    raise RuntimeError(f"Native desktop preparation failed: {error}.{detail}") from error
    if not _runtime_ready(executable):
        raise RuntimeError("Native desktop build did not produce the complete runtime.")
    return executable
