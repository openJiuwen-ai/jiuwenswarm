# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PowerPoint's per-user sideload registration.

macOS reads every manifest in the ``wef`` folder of PowerPoint's sandbox
container; Windows reads the manifest paths under ``Wef\\Developer`` in the
user's registry. Both are where Microsoft's own sideload tooling registers.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol

# The pane's dev server origin, which the source manifest names.
DEV_ORIGIN = "https://localhost:3001"
# Distinct from the source manifest's Id, so a development sideload and the
# installed add-in do not replace each other's registration.
ADDIN_ID = "340428ab-de7e-43fd-b7ca-d77377bb73d5"
MANIFEST_NAME = "manifest.powerpoint.xml"
_ID = re.compile(r"<Id>[^<]*</Id>")


def render_manifest(template: str, origin: str) -> str:
    """The source manifest pointed at ``origin``, where the pane is served under ``/office/``."""
    text = template.replace(f"{DEV_ORIGIN}/", f"{origin}/office/").replace(DEV_ORIGIN, origin)
    return _ID.sub(f"<Id>{ADDIN_ID}</Id>", text, count=1)


class Registration(Protocol):
    def host_found(self) -> bool:
        ...

    def read(self) -> str | None:
        """The registered manifest, or None when the add-in is not registered."""
        ...

    def write(self, manifest: str) -> None:
        ...

    def delete(self) -> None:
        ...


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class MacRegistration:
    def __init__(self, home: Path) -> None:
        self._container = home / "Library" / "Containers" / "com.microsoft.Powerpoint"
        self.path = self._container / "Data" / "Documents" / "wef" / "jiuwenswarm-powerpoint.xml"

    def host_found(self) -> bool:
        # PowerPoint creates its container on first launch; never create it for an app that has not run.
        return self._container.is_dir()

    def read(self) -> str | None:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else None

    def write(self, manifest: str) -> None:
        _write_atomic(self.path, manifest)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class Registry(Protocol):
    """String values under ``HKEY_CURRENT_USER``."""

    def get(self, key: str, name: str) -> str | None:
        ...

    def set(self, key: str, name: str, value: str) -> None:
        ...

    def delete(self, key: str, name: str) -> None:
        ...


class WinRegistry:
    def __init__(self) -> None:
        import winreg

        self._winreg = winreg

    def get(self, key: str, name: str) -> str | None:
        try:
            with self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, key) as handle:
                return str(self._winreg.QueryValueEx(handle, name)[0])
        except FileNotFoundError:
            return None

    def set(self, key: str, name: str, value: str) -> None:
        with self._winreg.CreateKey(self._winreg.HKEY_CURRENT_USER, key) as handle:
            self._winreg.SetValueEx(handle, name, 0, self._winreg.REG_SZ, value)

    def delete(self, key: str, name: str) -> None:
        try:
            with self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER, key, 0, self._winreg.KEY_SET_VALUE,
            ) as handle:
                self._winreg.DeleteValue(handle, name)
        except FileNotFoundError:
            pass


class WindowsRegistration:
    """The registry value names the manifest file, which the desktop keeps in its data directory."""

    KEY = r"SOFTWARE\Microsoft\Office\16.0\Wef\Developer"

    def __init__(self, manifest_path: Path, registry: Registry) -> None:
        self.path = manifest_path
        self._registry = registry

    @staticmethod
    def host_found() -> bool:
        # A value Office never reads is harmless, so there is nothing to detect.
        return True

    def read(self) -> str | None:
        if self._registry.get(self.KEY, ADDIN_ID) is None:
            return None
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else ""

    def write(self, manifest: str) -> None:
        _write_atomic(self.path, manifest)
        self._registry.set(self.KEY, ADDIN_ID, str(self.path))

    def delete(self) -> None:
        self._registry.delete(self.KEY, ADDIN_ID)
        self.path.unlink(missing_ok=True)
