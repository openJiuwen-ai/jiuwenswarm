# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Installs the desktop's task panes into the Office and WPS apps on this machine.

What is installed is never recorded here: each call reads the host's own
registration and the certificate files again, and every step only fills in
what is missing, so running install again is the retry.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sys
import threading
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar

from jiuwenswarm.channels.desktop.office_addins import certificate
from jiuwenswarm.channels.desktop.office_addins.certificate import TlsFiles, TrustStore
from jiuwenswarm.channels.desktop.office_addins.files import write_atomic
from jiuwenswarm.channels.desktop.office_addins.listener import TlsListener
from jiuwenswarm.channels.desktop.office_addins.registration import (
    HOSTS,
    MacRegistration,
    Registration,
    WindowsRegistration,
    WinRegistry,
    WpsRegistration,
    WPS_MAC_EDITIONS,
    WPS_TYPES,
    WPS_WINDOWS_EDITIONS,
    manifest_name,
    render_manifest,
    wps_url,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

Problem = Literal[
    "permission_denied",
    "certificate_untrusted",
    "certificate_expiring",
    "tls_unavailable",
    "stale_origin",
]

# An app this machine does not have yet is not a problem of the suite: the suite installs into the
# apps it finds, and installing again after the app has run once adds it. An app older than its
# manifest requires would never load the pane, so the suite leaves it out the same way.
AppState = Literal["installed", "not_installed", "host_not_found", "host_outdated"]


@dataclass(frozen=True)
class Suite:
    apps: tuple[str, ...]
    # Office loads panes only over HTTPS, from a certificate the system trusts; WPS loads them over HTTP from localhost.
    https: bool


# What the user installs is a suite: one card, one install for all of its apps. An app joins its
# suite when its pane lands.
SUITES: Mapping[str, Suite] = {
    "office": Suite(("powerpoint", "word", "excel"), https=True),
    "wps": Suite(tuple(WPS_TYPES), https=False),
}


@dataclass(frozen=True)
class SuiteStatus:
    """``installed`` once every app of the suite on this machine is registered, and at least one is."""

    installed: bool
    problems: tuple[Problem, ...]
    apps: dict[str, AppState]
    # The version each ``host_outdated`` app must be updated to.
    required: dict[str, str] = field(default_factory=dict)


def _user_id_file(workspace: Path) -> Path:
    return workspace / "office-addin" / "user-id"


_USER_ID = re.compile(r"office-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _read_user_id(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    return text if _USER_ID.fullmatch(text) else None


def _user_id(path: Path) -> str:
    """The one user id every host's pane connects as, created once and kept across reinstalls.

    The server scopes documents and conversations by user, so one id lets a pane in one host see and
    message the documents open in the others.
    """
    user_id = _read_user_id(path)
    if user_id is None:
        user_id = f"office-{uuid.uuid4()}"
        write_atomic(path, user_id.encode("utf-8"))
    return user_id


def _machine(workspace: Path) -> tuple[TlsFiles, TrustStore, dict[str, Registration]]:
    tls = TlsFiles(workspace / "office-addin" / "tls")
    registrations: dict[str, Registration]
    if sys.platform == "win32":
        registry = WinRegistry()
        trust: TrustStore = certificate.WindowsTrustStore()
        registrations = {
            app: WindowsRegistration(workspace / "office-addin", registry, app, registry.office_version)
            for app in HOSTS
        }
        wps_base, wps_editions = Path(os.environ["APPDATA"]), WPS_WINDOWS_EDITIONS
    else:
        home = Path.home()
        trust = certificate.MacTrustStore(home)
        registrations = {app: MacRegistration(home, app) for app in HOSTS}
        wps_base, wps_editions = home, WPS_MAC_EDITIONS
    registrations.update({app: WpsRegistration(wps_base, wps_editions, app) for app in WPS_TYPES})
    return tls, trust, registrations


def _release_certificate(tls: TlsFiles, trust: TrustStore) -> bool:
    """Untrust and delete the certificate; False, keeping the files to try again, while it stays trusted."""
    if tls.exist():
        trust.untrust(tls)
        if trust.is_trusted(tls):
            return False
    tls.delete()
    return True


def _per_app(apps: Iterable[str], step: Callable[[str], T], refused: list[str]) -> dict[str, T]:
    """``step`` for each app; an app whose step fails (a refused or unreadable file or registry) joins ``refused``.

    One app's failure never stops the others.
    """
    results: dict[str, T] = {}
    for app in apps:
        try:
            results[app] = step(app)
        except Exception as exc:
            logger.warning("[office-addins] %s: %s", app, exc)
            refused.append(app)
    return results


def remove_all(workspace: Path) -> bool:
    """What the Windows uninstaller runs; False, logging the certificate's path, while the system still trusts it."""
    tls, trust, registrations = _machine(workspace)
    for registration in registrations.values():
        registration.delete()
    released = _release_certificate(tls, trust)
    if not released:
        logger.warning("[office-addins] %s is still a trusted root certificate; remove it by hand", tls.ca)
    _user_id_file(workspace).unlink(missing_ok=True)
    return released


class OfficeAddins:
    """The add-ins this desktop process serves over ``listener``."""

    def __init__(
        self,
        *,
        tls: TlsFiles,
        trust: TrustStore,
        registrations: Mapping[str, Registration],
        listener: TlsListener,
        http_origin: str,
        office_dist: Path,
        user_id_file: Path,
    ) -> None:
        self._tls = tls
        self._trust = trust
        self._registrations = registrations
        # The suites this machine knows where to register every app of.
        self.suites = {name: suite for name, suite in SUITES.items() if set(suite.apps) <= registrations.keys()}
        self._listener = listener
        self._http_origin = http_origin
        self._office_dist = office_dist
        self._user_id_file = user_id_file
        # Install, remove and the startup rewrite all change the same files and registrations.
        self._lock = threading.Lock()

    @classmethod
    def for_this_machine(
        cls, workspace: Path, listener: TlsListener, http_origin: str, office_dist: Path,
    ) -> OfficeAddins:
        tls, trust, registrations = _machine(workspace)
        return cls(
            tls=tls,
            trust=trust,
            registrations=registrations,
            listener=listener,
            http_origin=http_origin,
            office_dist=office_dist,
            user_id_file=_user_id_file(workspace),
        )

    def status(self) -> dict[str, SuiteStatus]:
        # Without the lock, which install holds through a trust prompt of up to minutes. Every file install
        # writes is replaced in one step, so a status read meanwhile shows how far that install has got.
        return {suite: self._status(suite) for suite in self.suites}

    def install(self, suite: str) -> SuiteStatus:
        with self._lock:
            refused: list[str] = []
            hosts = _per_app(self.suites[suite].apps, self._ready, refused)
            apps = [app for app, found in hosts.items() if found]
            if not apps or (self.suites[suite].https and not self._serve_https()):
                return self._status(suite, refused)
            user_id = _user_id(self._user_id_file)
            _per_app(apps, lambda app: self._registrations[app].write(self._manifest(app, user_id)), refused)
            return self._status(suite, refused)

    def remove(self, suite: str) -> SuiteStatus:
        with self._lock:
            last = self.suites[suite].https and self._no_other_https_registered(suite)
            # Untrusting first: when the system keeps the trust (a declined prompt), the suite stays installed
            # and Remove can run again.
            if last and not _release_certificate(self._tls, self._trust):
                return self._status(suite)
            refused: list[str] = []
            _per_app(self.suites[suite].apps, lambda app: self._registrations[app].delete(), refused)
            if last:
                self._listener.close()
            return self._status(suite, refused)

    def _no_other_https_registered(self, suite: str) -> bool:
        """Whether removing ``suite`` leaves no registration needing the certificate; an unreadable one might."""
        unreadable: list[str] = []
        certified = [other for name, other in self.suites.items() if other.https and name != suite]
        others = [app for other in certified for app in other.apps]
        registered = _per_app(others, lambda app: self._registrations[app].read(), unreadable)
        return not unreadable and all(manifest is None for manifest in registered.values())

    def ensure_registered(self) -> None:
        """Serve the panes again after a restart and point every registration at this process's origin."""
        with self._lock:
            serving = self._tls.exist() and self._listener.serve(self._tls)
            suites = [suite for suite in self.suites.values() if serving or not suite.https]
            _per_app([app for suite in suites for app in suite.apps], self._restore, [])

    def _restore(self, app: str) -> None:
        self._registrations[app].rewrite(lambda: self._manifest(app, _user_id(self._user_id_file)))

    def _serve_https(self) -> bool:
        """A trusted certificate, renewed when expiring, and the HTTPS listener serving with it."""
        now = dt.datetime.now(dt.timezone.utc)
        if not self._tls.exist():
            certificate.generate(self._tls, now)
        elif self._tls.expiring(now) and not self._renew(now):
            return False
        if not self._trust.is_trusted(self._tls) and not self._trust.trust(self._tls):
            return False
        return self._listener.serve(self._tls)

    def _renew(self, now: dt.datetime) -> bool:
        """Trust a new certificate before untrusting the old one, so a declined prompt leaves the old one serving."""
        renewed = TlsFiles(self._tls.directory / "renewed")
        certificate.generate(renewed, now)
        if not self._trust.trust(renewed):
            renewed.delete()
            return False
        # The old CA's key was never written and it expires within days, so a failed untrust is not worth stopping.
        self._trust.untrust(self._tls)
        self._tls.take(renewed)
        return True

    def _manifest(self, app: str, user_id: str) -> str:
        """What ``app``'s registration holds while current: the Office manifest, or the URL WPS loads."""
        if app in WPS_TYPES:
            return wps_url(app, self._http_origin)
        template = (self._office_dist / manifest_name(app)).read_text(encoding="utf-8")
        return render_manifest(app, template, self._listener.origin, user_id)

    def _ready(self, app: str) -> bool:
        """Whether ``app`` is here and new enough to load its pane."""
        registration = self._registrations[app]
        return registration.host_found() and registration.outdated() is None

    def _observe(self, app: str, user_id: str | None) -> tuple[bool, str | None, str | None, bool]:
        """Whether the host is here, the version it must be updated to, its registration, and whether that points
        elsewhere than this process.
        """
        registration = self._registrations[app]
        registered = registration.read()
        # WPS urls carry no user id; an Office manifest is current only with this install's one.
        stale = registered is not None and registered != self._manifest(app, user_id or "")
        return registration.host_found(), registration.outdated(), registered, stale

    def _status(self, suite: str, refused: Collection[str] = ()) -> SuiteStatus:
        """``refused``: apps whose registration the system refused to write or delete just now."""
        unreadable: list[str] = []
        user_id = _read_user_id(self._user_id_file)
        observed = _per_app(self.suites[suite].apps, lambda app: self._observe(app, user_id), unreadable)
        found: list[Problem] = []
        apps: dict[str, AppState] = {}
        required: dict[str, str] = {}
        for app in self.suites[suite].apps:
            host_found, outdated, registered, stale = observed.get(app, (True, None, None, False))
            if app in refused or app in unreadable:
                found.append("permission_denied")
            if not host_found:
                apps[app] = "host_not_found"
                continue
            if outdated is not None:
                apps[app], required[app] = "host_outdated", outdated
                continue
            if stale:
                found.append("stale_origin")
            apps[app] = "not_installed" if registered is None else "installed"
        if self.suites[suite].https:
            found += self._https_problems(registered="installed" in apps.values())
        states = set(apps.values())
        installed = "installed" in states and "not_installed" not in states
        return SuiteStatus(installed=installed, problems=tuple(dict.fromkeys(found)), apps=apps, required=required)

    def _https_problems(self, *, registered: bool) -> list[Problem]:
        if not self._tls.exist():
            # Registered panes cannot load without a trusted certificate; installing again makes and trusts one.
            return ["certificate_untrusted"] if registered else []
        problems: list[Problem] = []
        if not self._trust.is_trusted(self._tls):
            problems.append("certificate_untrusted")
        if self._tls.expiring(dt.datetime.now(dt.timezone.utc)):
            problems.append("certificate_expiring")
        if not self._listener.serving:
            problems.append("tls_unavailable")
        return problems
