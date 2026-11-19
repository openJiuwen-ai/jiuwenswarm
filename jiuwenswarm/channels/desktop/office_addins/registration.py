# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Each Office host's per-user sideload registration.

macOS reads every manifest in the ``wef`` folder of the host's sandbox
container; Windows reads the manifest paths under ``Wef\\Developer`` in the
user's registry, one value per add-in Id. Both are where Microsoft's own
sideload tooling (office-addin-dev-settings) registers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jiuwenswarm.channels.desktop.office_addins.files import write_atomic

# The pane's dev server origin, which the source manifests name.
DEV_ORIGIN = "https://localhost:3001"
_ID = re.compile(r"<Id>[^<]*</Id>")
_PANE_URL = re.compile(r'(/taskpane\.\w+\.html)"')


@dataclass(frozen=True)
class Host:
    # Distinct from the source manifest's Id, so a development sideload and the
    # installed add-in do not replace each other's registration.
    addin_id: str
    # The sandbox container (the app's bundle id) whose Data/Documents/wef folder the host reads on macOS.
    mac_container: str


HOSTS: Mapping[str, Host] = {
    "powerpoint": Host("340428ab-de7e-43fd-b7ca-d77377bb73d5", "com.microsoft.Powerpoint"),
    "word": Host("b65bfac2-6aec-4c86-acd7-a0be9f174e17", "com.microsoft.Word"),
    "excel": Host("59f438ed-5f60-4b45-bd97-7c8be28d6b8d", "com.microsoft.Excel"),
}


def manifest_name(app: str) -> str:
    return f"manifest.{app}.xml"


def render_manifest(app: str, template: str, origin: str, user_id: str) -> str:
    """``app``'s source manifest pointed at ``origin``, where the pane is served under ``/office/``.

    The pane URL carries ``user_id``, the one user every Office host's pane connects as.
    """
    text = template.replace(f"{DEV_ORIGIN}/", f"{origin}/office/").replace(DEV_ORIGIN, origin)
    text = _PANE_URL.sub(rf'\1?u={user_id}"', text)
    return _ID.sub(f"<Id>{HOSTS[app].addin_id}</Id>", text, count=1)


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


class MacRegistration:
    def __init__(self, home: Path, app: str) -> None:
        self._container = home / "Library" / "Containers" / HOSTS[app].mac_container
        self.path = self._container / "Data" / "Documents" / "wef" / f"jiuwenswarm-{app}.xml"

    def host_found(self) -> bool:
        # The app creates its container on first launch; never create it for an app that has not run.
        return self._container.is_dir()

    def read(self) -> str | None:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else None

    def write(self, manifest: str) -> None:
        write_atomic(self.path, manifest.encode("utf-8"))

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

    def __init__(self, directory: Path, registry: Registry, app: str) -> None:
        self.path = directory / manifest_name(app)
        self._registry = registry
        self._addin_id = HOSTS[app].addin_id

    @staticmethod
    def host_found() -> bool:
        # A value Office never reads is harmless, so there is nothing to detect.
        return True

    def read(self) -> str | None:
        if self._registry.get(self.KEY, self._addin_id) is None:
            return None
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else ""

    def write(self, manifest: str) -> None:
        write_atomic(self.path, manifest.encode("utf-8"))
        self._registry.set(self.KEY, self._addin_id, str(self.path))

    def delete(self) -> None:
        self._registry.delete(self.KEY, self._addin_id)
        self.path.unlink(missing_ok=True)
