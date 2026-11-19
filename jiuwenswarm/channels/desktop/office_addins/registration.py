# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Each host's per-user add-in registration.

Office on macOS reads every manifest in the ``wef`` folder of the host's sandbox
container; on Windows it reads the manifest paths under ``Wef\\Developer`` in the
user's registry, one value per add-in Id. Both are where Microsoft's own
sideload tooling (office-addin-dev-settings) registers. WPS reads the add-ins
it loads from its ``publish.xml``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
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

    def rewrite(self, manifest: Callable[[], str]) -> None:
        """Point what is registered at ``manifest()``, registering nothing where nothing is."""
        ...

    def delete(self) -> None:
        ...


def _rewrite(registration: Registration | _WpsList, manifest: Callable[[], str]) -> None:
    registered = registration.read()
    if registered is None:
        return
    current = manifest()
    if registered != current:
        registration.write(current)


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

    def rewrite(self, manifest: Callable[[], str]) -> None:
        _rewrite(self, manifest)

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

    def rewrite(self, manifest: Callable[[], str]) -> None:
        _rewrite(self, manifest)

    def delete(self) -> None:
        self._registry.delete(self.KEY, self._addin_id)
        self.path.unlink(missing_ok=True)


# The add-in type WPS gives each of its apps, which is also the folder of that app's shell under ``/office/wps/``.
WPS_TYPES: Mapping[str, str] = {"wps-presentation": "wpp", "wps-writer": "wps", "wps-spreadsheet": "et"}


@dataclass(frozen=True)
class WpsEdition:
    # The folder the edition creates on its first launch, relative to the home folder on Mac and to %APPDATA% on
    # Windows; nothing here creates it for an edition that has not run.
    folder: str
    # The edition's add-in list, inside ``folder``.
    publish: str


# Each WPS edition's add-in list. On Mac each edition has its own sandbox container.
WPS_MAC_EDITIONS: Mapping[str, WpsEdition] = {
    "international": WpsEdition(
        "Library/Containers/com.kingsoft.wpsoffice.mac.global", "Data/.kingsoft/wps/jsaddons/publish.xml",
    ),
    "domestic": WpsEdition("Library/Containers/com.kingsoft.wpsoffice.mac", "Data/.kingsoft/wps/jsaddons/publish.xml"),
}
# On Windows the China edition keeps it in %APPDATA%\kingsoft, where WPS's own add-in tool (wpsjs) writes it. That the
# international edition shares that vendor folder is inferred, not verified.
WPS_WINDOWS_EDITIONS: Mapping[str, WpsEdition] = {
    "international": WpsEdition("kingsoft", "wps/jsaddons/publish.xml"),
    "domestic": WpsEdition("kingsoft", "wps/jsaddons/publish.xml"),
}
_WPS_NAME = "WorkSwarm"
_WPS_ENTRY = re.compile(r"\s*<jspluginonline\b([^>]*?)/?>(?:\s*</jspluginonline>)?")
_ATTRIBUTE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_ENCODING = re.compile(r"""<\?xml[^>]*encoding=["']([\w.-]+)["']""")


def wps_url(app: str, origin: str) -> str:
    """The folder of ``app``'s shell (``ribbon.xml``, ``index.html``); WPS loads it over HTTP from localhost."""
    return f"{origin}/office/wps/{WPS_TYPES[app]}/"


class WpsRegistration:
    """``app``'s entry in the add-in list of each WPS edition on this machine."""

    def __init__(self, base: Path, editions: Mapping[str, WpsEdition], app: str) -> None:
        # Editions sharing one list share its entry.
        lists = dict.fromkeys((base / edition.folder, edition.publish) for edition in editions.values())
        self._lists = [_WpsList(folder, folder / publish, WPS_TYPES[app]) for folder, publish in lists]

    def host_found(self) -> bool:
        return bool(self._found())

    def read(self) -> str | None:
        """The URL our entries load: None while no edition holds one, and stale ("") while editions disagree.

        An edition that ran first after the install holds no entry, which leaves the app registered.
        """
        urls = {wps_list.read() for wps_list in self._found()} - {None}
        if not urls:
            return None
        return urls.pop() if len(urls) == 1 else ""

    def write(self, manifest: str) -> None:
        _each(self._found(), lambda wps_list: wps_list.write(manifest))

    def rewrite(self, manifest: Callable[[], str]) -> None:
        _each(self._found(), lambda wps_list: _rewrite(wps_list, manifest))

    def delete(self) -> None:
        _each(self._lists, lambda wps_list: wps_list.delete())

    def _found(self) -> list[_WpsList]:
        return [wps_list for wps_list in self._lists if wps_list.folder.is_dir()]


def _each(wps_lists: list[_WpsList], step: Callable[[_WpsList], None]) -> None:
    """``step`` on every edition, so a list the system refuses leaves the others changed; then its error."""
    failure: OSError | None = None
    for wps_list in wps_lists:
        try:
            step(wps_list)
        except OSError as exc:
            failure = failure or exc
    if failure is not None:
        raise failure


class _WpsList:
    """One edition's ``publish.xml``, which lists every app's add-ins; only our entry of ``app_type`` changes."""

    def __init__(self, folder: Path, path: Path, app_type: str) -> None:
        self.folder = folder
        self.path = path
        self._type = app_type

    def read(self) -> str | None:
        """The URL our entry loads, or None without one."""
        ours = self._ours(self._load()[0])
        return dict(_ATTRIBUTE.findall(ours[0].group(1))).get("url", "") if ours else None

    def write(self, manifest: str) -> None:
        text, encoding = self._load()
        text = self._without_ours(text)
        close = text.rfind("</jsplugins>")
        if close < 0:
            # No list yet, or WPS's empty one (<jsplugins/>), which holds nothing to keep.
            text = f'<?xml version="1.0" encoding="{encoding}"?>\n<jsplugins>\n</jsplugins>\n'
            close = text.rfind("</jsplugins>")
        entry = (
            f'<jspluginonline enable="enable_dev" type="{self._type}" name="{_WPS_NAME}" install="null"'
            f' url="{manifest}"/>'
        )
        # After the last entry, before the whitespace that ends it, so delete leaves the file as it was.
        at = len(text[:close].rstrip())
        write_atomic(self.path, f"{text[:at]}\n    {entry}{text[at:]}".encode(encoding))

    def delete(self) -> None:
        text, encoding = self._load()
        kept = self._without_ours(text)
        if kept != text:
            write_atomic(self.path, kept.encode(encoding))

    def _load(self) -> tuple[str, str]:
        """The list's text and the encoding it declares; an empty list when WPS has not written one yet."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return "", "UTF-8"
        declared = _ENCODING.search(data[:200].decode("ascii", errors="replace"))
        encoding = declared.group(1) if declared else "UTF-8"
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError) as exc:
            raise OSError(f"{self.path} is not {encoding}") from exc

    def _ours(self, text: str) -> list[re.Match[str]]:
        def ours(attributes: dict[str, str]) -> bool:
            return attributes.get("type") == self._type and attributes.get("name") == _WPS_NAME

        return [entry for entry in _WPS_ENTRY.finditer(text) if ours(dict(_ATTRIBUTE.findall(entry.group(1))))]

    def _without_ours(self, text: str) -> str:
        for entry in reversed(self._ours(text)):
            text = text[: entry.start()] + text[entry.end():]
        return text
