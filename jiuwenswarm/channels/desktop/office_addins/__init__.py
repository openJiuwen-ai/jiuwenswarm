# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Installs the desktop's task panes into Office hosts on this machine.

What is installed is never recorded here: each call reads the host's own
registration and the certificate files again, and every step only fills in
what is missing, so running install again is the retry.
"""

from __future__ import annotations

import datetime as dt
import sys
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jiuwenswarm.channels.desktop.office_addins import certificate, powerpoint
from jiuwenswarm.channels.desktop.office_addins.certificate import TlsFiles, TrustStore
from jiuwenswarm.channels.desktop.office_addins.listener import TlsListener
from jiuwenswarm.channels.desktop.office_addins.powerpoint import Registration

Problem = Literal[
    "host_not_found",
    "permission_denied",
    "certificate_untrusted",
    "certificate_expiring",
    "tls_unavailable",
    "stale_origin",
]

# What the user installs is a suite: one card, one install for all of its apps. An app joins its
# suite when its pane lands.
SUITES: Mapping[str, tuple[str, ...]] = {"office": ("powerpoint",)}
# Each app's manifest renderer: (source manifest, origin) -> the manifest to register.
RENDERERS: Mapping[str, Callable[[str, str], str]] = {"powerpoint": powerpoint.render_manifest}


@dataclass(frozen=True)
class SuiteStatus:
    """``installed`` once every app of the suite is registered; ``apps`` says which ones are."""

    installed: bool
    problems: tuple[Problem, ...]
    apps: dict[str, bool]


def _machine(workspace: Path) -> tuple[TlsFiles, TrustStore, dict[str, Registration]]:
    tls = TlsFiles(workspace / "office-addin" / "tls")
    if sys.platform == "win32":
        manifest = workspace / "office-addin" / powerpoint.MANIFEST_NAME
        registry = powerpoint.WinRegistry()
        return tls, certificate.WindowsTrustStore(), {"powerpoint": powerpoint.WindowsRegistration(manifest, registry)}
    home = Path.home()
    return tls, certificate.MacTrustStore(home), {"powerpoint": powerpoint.MacRegistration(home)}


def _release_certificate(tls: TlsFiles, trust: TrustStore) -> None:
    if tls.ca.is_file():
        trust.untrust(tls)
    tls.delete()


def remove_all(workspace: Path) -> None:
    """Remove every host's registration and the certificate; the Windows uninstaller runs this."""
    tls, trust, registrations = _machine(workspace)
    for registration in registrations.values():
        registration.delete()
    _release_certificate(tls, trust)


class OfficeAddins:
    """The add-ins this desktop process serves over ``listener``."""

    def __init__(
        self,
        *,
        tls: TlsFiles,
        trust: TrustStore,
        registrations: Mapping[str, Registration],
        listener: TlsListener,
        office_dist: Path,
    ) -> None:
        self._tls = tls
        self._trust = trust
        self._registrations = registrations
        self._listener = listener
        self._office_dist = office_dist
        # Install, remove and the startup rewrite all change the same files and registrations.
        self._lock = threading.Lock()

    @classmethod
    def for_this_machine(cls, workspace: Path, listener: TlsListener, office_dist: Path) -> OfficeAddins:
        tls, trust, registrations = _machine(workspace)
        return cls(tls=tls, trust=trust, registrations=registrations, listener=listener, office_dist=office_dist)

    def status(self) -> dict[str, SuiteStatus]:
        with self._lock:
            return {suite: self._status(suite) for suite in SUITES}

    def install(self, suite: str) -> SuiteStatus:
        with self._lock:
            apps = [app for app in SUITES[suite] if self._registrations[app].host_found()]
            if not apps:
                return self._status(suite)
            now = dt.datetime.now(dt.timezone.utc)
            if not self._tls.exist() or self._tls.expiring(now):
                _release_certificate(self._tls, self._trust)
                certificate.generate(self._tls, now)
            if not self._trust.is_trusted(self._tls) and not self._trust.trust(self._tls):
                return self._status(suite)
            if not self._listener.serve(self._tls):
                return self._status(suite)
            denied = []
            for app in apps:
                try:
                    self._registrations[app].write(self._manifest(app))
                except PermissionError:
                    denied.append(app)
            return self._status(suite, denied)

    def remove(self, suite: str) -> SuiteStatus:
        with self._lock:
            denied = []
            for app in SUITES[suite]:
                try:
                    self._registrations[app].delete()
                except PermissionError:
                    denied.append(app)
            if all(registration.read() is None for registration in self._registrations.values()):
                _release_certificate(self._tls, self._trust)
                self._listener.close()
            return self._status(suite, denied)

    def ensure_registered(self) -> None:
        """Serve the panes again after a restart and point every registration at this process's origin."""
        with self._lock:
            if not self._tls.exist() or not self._listener.serve(self._tls):
                return
            for app, registration in self._registrations.items():
                registered = registration.read()
                manifest = self._manifest(app)
                if registered is not None and registered != manifest:
                    registration.write(manifest)

    def _manifest(self, app: str) -> str:
        template = (self._office_dist / f"manifest.{app}.xml").read_text(encoding="utf-8")
        return RENDERERS[app](template, self._listener.origin)

    def _status(self, suite: str, denied: Collection[str] = ()) -> SuiteStatus:
        """``denied``: apps whose registration could not be written or deleted just now."""
        found: list[Problem] = []
        apps: dict[str, bool] = {}
        for app in SUITES[suite]:
            registration = self._registrations[app]
            if app in denied:
                found.append("permission_denied")
            if not registration.host_found():
                found.append("host_not_found")
            try:
                registered = registration.read()
            except PermissionError:
                registered = None
                found.append("permission_denied")
            if registered is not None and registered != self._manifest(app):
                found.append("stale_origin")
            apps[app] = registered is not None
        if self._tls.exist():
            if not self._trust.is_trusted(self._tls):
                found.append("certificate_untrusted")
            if self._tls.expiring(dt.datetime.now(dt.timezone.utc)):
                found.append("certificate_expiring")
            if not self._listener.serving:
                found.append("tls_unavailable")
        return SuiteStatus(installed=all(apps.values()), problems=tuple(dict.fromkeys(found)), apps=apps)
