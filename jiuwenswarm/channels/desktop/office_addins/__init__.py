# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Installs the desktop's task panes into Office hosts on this machine.

What is installed is never recorded here: each call reads the host's own
registration and the certificate files again, and every step only fills in
what is missing, so running install again is the retry.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sys
import threading
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
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
    manifest_name,
    render_manifest,
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
# apps it finds, and installing again after the app has run once adds it.
AppState = Literal["installed", "not_installed", "host_not_found"]

# What the user installs is a suite: one card, one install for all of its apps. An app joins its
# suite when its pane lands.
SUITES: Mapping[str, tuple[str, ...]] = {"office": ("powerpoint", "word", "excel")}


@dataclass(frozen=True)
class SuiteStatus:
    """``installed`` once every app of the suite on this machine is registered, and at least one is."""

    installed: bool
    problems: tuple[Problem, ...]
    apps: dict[str, AppState]


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
    if sys.platform == "win32":
        registry = WinRegistry()
        return tls, certificate.WindowsTrustStore(), {
            app: WindowsRegistration(workspace / "office-addin", registry, app) for app in HOSTS
        }
    home = Path.home()
    return tls, certificate.MacTrustStore(home), {app: MacRegistration(home, app) for app in HOSTS}


def _release_certificate(tls: TlsFiles, trust: TrustStore) -> bool:
    """Untrust and delete the certificate; False, keeping the files to try again, while it stays trusted."""
    if tls.exist():
        trust.untrust(tls)
        if trust.is_trusted(tls):
            return False
    tls.delete()
    return True


def _per_app(apps: Iterable[str], step: Callable[[str], T], refused: list[str]) -> dict[str, T]:
    """``step`` for each app; an app whose files or registry the system refuses joins ``refused``."""
    results: dict[str, T] = {}
    for app in apps:
        try:
            results[app] = step(app)
        except OSError as exc:
            logger.warning("[office-addins] %s: %s", app, exc)
            refused.append(app)
    return results


def remove_all(workspace: Path) -> None:
    """Remove every host's registration, the certificate and the user id; the Windows uninstaller runs this."""
    tls, trust, registrations = _machine(workspace)
    for registration in registrations.values():
        registration.delete()
    _release_certificate(tls, trust)
    _user_id_file(workspace).unlink(missing_ok=True)


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
        user_id_file: Path,
    ) -> None:
        self._tls = tls
        self._trust = trust
        self._registrations = registrations
        self._listener = listener
        self._office_dist = office_dist
        self._user_id_file = user_id_file
        # Install, remove and the startup rewrite all change the same files and registrations.
        self._lock = threading.Lock()

    @classmethod
    def for_this_machine(cls, workspace: Path, listener: TlsListener, office_dist: Path) -> OfficeAddins:
        tls, trust, registrations = _machine(workspace)
        return cls(
            tls=tls,
            trust=trust,
            registrations=registrations,
            listener=listener,
            office_dist=office_dist,
            user_id_file=_user_id_file(workspace),
        )

    def status(self) -> dict[str, SuiteStatus]:
        # Without the lock, which install holds through a trust prompt of up to minutes. Every file install
        # writes is replaced in one step, so a status read meanwhile shows how far that install has got.
        return {suite: self._status(suite) for suite in SUITES}

    def install(self, suite: str) -> SuiteStatus:
        with self._lock:
            refused: list[str] = []
            hosts = _per_app(SUITES[suite], lambda app: self._registrations[app].host_found(), refused)
            apps = [app for app, found in hosts.items() if found]
            if not apps:
                return self._status(suite, refused)
            now = dt.datetime.now(dt.timezone.utc)
            if not self._tls.exist():
                certificate.generate(self._tls, now)
            elif self._tls.expiring(now) and not self._renew(now):
                return self._status(suite, refused)
            if not self._trust.is_trusted(self._tls) and not self._trust.trust(self._tls):
                return self._status(suite, refused)
            if not self._listener.serve(self._tls):
                return self._status(suite, refused)
            user_id = _user_id(self._user_id_file)
            _per_app(apps, lambda app: self._registrations[app].write(self._manifest(app, user_id)), refused)
            return self._status(suite, refused)

    def remove(self, suite: str) -> SuiteStatus:
        with self._lock:
            unreadable: list[str] = []
            others = [app for app in self._registrations if app not in SUITES[suite]]
            registered = _per_app(others, lambda app: self._registrations[app].read(), unreadable)
            last = not unreadable and all(manifest is None for manifest in registered.values())
            # Untrusting first: when the system keeps the trust (a declined prompt), the suite stays installed
            # and Remove can run again.
            if last and not _release_certificate(self._tls, self._trust):
                return self._status(suite)
            refused: list[str] = []
            _per_app(SUITES[suite], lambda app: self._registrations[app].delete(), refused)
            if last:
                self._listener.close()
            return self._status(suite, refused)

    def ensure_registered(self) -> None:
        """Serve the panes again after a restart and point every registration at this process's origin."""
        with self._lock:
            if not self._tls.exist() or not self._listener.serve(self._tls):
                return
            user_id = _user_id(self._user_id_file)
            _per_app(self._registrations, lambda app: self._restore(app, user_id), [])

    def _restore(self, app: str, user_id: str) -> None:
        registration = self._registrations[app]
        registered = registration.read()
        manifest = self._manifest(app, user_id)
        if registered is not None and registered != manifest:
            registration.write(manifest)

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
        template = (self._office_dist / manifest_name(app)).read_text(encoding="utf-8")
        return render_manifest(app, template, self._listener.origin, user_id)

    def _observe(self, app: str) -> tuple[bool, str | None]:
        registration = self._registrations[app]
        return registration.host_found(), registration.read()

    def _status(self, suite: str, refused: Collection[str] = ()) -> SuiteStatus:
        """``refused``: apps whose registration the system refused to write or delete just now."""
        unreadable: list[str] = []
        observed = _per_app(SUITES[suite], self._observe, unreadable)
        user_id = _read_user_id(self._user_id_file)
        found: list[Problem] = []
        apps: dict[str, AppState] = {}
        for app in SUITES[suite]:
            host_found, registered = observed.get(app, (True, None))
            if app in refused or app in unreadable:
                found.append("permission_denied")
            if not host_found:
                apps[app] = "host_not_found"
                continue
            if registered is not None and (user_id is None or registered != self._manifest(app, user_id)):
                found.append("stale_origin")
            apps[app] = "not_installed" if registered is None else "installed"
        if self._tls.exist():
            if not self._trust.is_trusted(self._tls):
                found.append("certificate_untrusted")
            if self._tls.expiring(dt.datetime.now(dt.timezone.utc)):
                found.append("certificate_expiring")
            if not self._listener.serving:
                found.append("tls_unavailable")
        elif "installed" in apps.values():
            # Registered panes cannot load without a trusted certificate; installing again makes and trusts one.
            found.append("certificate_untrusted")
        states = set(apps.values())
        installed = "installed" in states and "not_installed" not in states
        return SuiteStatus(installed=installed, problems=tuple(dict.fromkeys(found)), apps=apps)
