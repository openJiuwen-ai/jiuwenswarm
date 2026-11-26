"""Office add-in installation against fake trust stores, a fake registry and a temporary home."""

from __future__ import annotations

import datetime as dt
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID

from jiuwenswarm.channels.desktop import office_addins
from jiuwenswarm.channels.desktop.office_addins import OfficeAddins, SuiteStatus, certificate
from jiuwenswarm.channels.desktop.office_addins.certificate import MacTrustStore, Runner, TlsFiles, WindowsTrustStore
from jiuwenswarm.channels.desktop.office_addins.listener import TlsListener
from jiuwenswarm.channels.desktop.office_addins.registration import (
    HOSTS,
    WPS_MAC_EDITIONS,
    WPS_WINDOWS_EDITIONS,
    MacRegistration,
    WindowsRegistration,
    WpsRegistration,
)

ROOT = Path(__file__).resolve().parents[2]
PANE_PACKAGE = ROOT / "packages" / "jiuwenswarm-office-addin"
APPS = ("powerpoint", "word", "excel")
WPS_APPS = ("wps-presentation", "wps-writer", "wps-spreadsheet")
HTTP_ORIGIN = "http://127.0.0.1:5173"
INSTALLED = SuiteStatus(installed=True, problems=(), apps=dict.fromkeys(APPS, "installed"))
NOT_INSTALLED = SuiteStatus(installed=False, problems=(), apps=dict.fromkeys(APPS, "not_installed"))


def _load(path: str | Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(Path(path).read_bytes())


def _sha1(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA1()).hex().upper()  # noqa: S324


def _issued_by(leaf: x509.Certificate, ca: x509.Certificate) -> bool:
    try:
        leaf.verify_directly_issued_by(ca)
    except Exception:  # noqa: BLE001 - any failure means "not this CA"
        return False
    return True


class FakeKeychain:
    """``security`` over an in-memory set of trusted roots; ``accept`` answers the password prompts."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.trusted: dict[str, x509.Certificate] = {}
        self.commands: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.commands.append(args)
        ok = True
        if args[1] == "add-trusted-cert":
            ok = self.accept
            if ok:
                cert = _load(args[-1])
                self.trusted[_sha1(cert)] = cert
        elif args[1] == "verify-cert":
            leaf = _load(args[args.index("-c") + 1])
            ok = any(_issued_by(leaf, ca) for ca in self.trusted.values())
        elif args[1] == "remove-trusted-cert":
            ok = self.accept
            if ok:
                self.trusted.pop(_sha1(_load(args[2])), None)
        elif args[1] == "delete-certificate":
            self.trusted.pop(args[args.index("-Z") + 1], None)
        return subprocess.CompletedProcess(args, 0 if ok else 1, "", "")

    def verbs(self) -> list[str]:
        return [command[1] for command in self.commands]


class FakeCertutil:
    def __init__(self) -> None:
        self.root: set[str] = set()

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        verb, target = args[2], args[-1]
        ok = True
        if verb == "-addstore":
            self.root.add(_sha1(_load(target)))
        elif verb == "-verifystore":
            ok = target in self.root
        elif verb == "-delstore":
            self.root.discard(target)
        return subprocess.CompletedProcess(args, 0 if ok else 1, "", "")


class FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, key: str, name: str) -> str | None:
        return self.values.get((key, name))

    def set(self, key: str, name: str, value: str) -> None:
        self.values[(key, name)] = value

    def delete(self, key: str, name: str) -> None:
        self.values.pop((key, name), None)

    @staticmethod
    def office_version() -> str | None:
        return None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def office_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist" / "office"
    dist.mkdir(parents=True)
    for app in APPS:
        shutil.copy(PANE_PACKAGE / f"manifest.{app}.xml", dist)
    return dist


@pytest.fixture
def listeners() -> Iterator[list[TlsListener]]:
    opened: list[TlsListener] = []
    yield opened
    for listener in opened:
        listener.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for host in HOSTS.values():
        (home / "Library" / "Containers" / host.mac_container).mkdir(parents=True)
    return home


def _mac_addins(
    home: Path, office_dist: Path, listeners: list[TlsListener], keychain: Runner, port: int | None = None,
    *, registrations: dict[str, MacRegistration] | None = None,
) -> OfficeAddins:
    listener = TlsListener(port or _free_port(), partial(SimpleHTTPRequestHandler, directory=str(office_dist.parent)))
    listeners.append(listener)
    return OfficeAddins(
        tls=TlsFiles(home / "data" / "office-addin" / "tls"),
        trust=MacTrustStore(home, runner=keychain),
        registrations=registrations or {app: MacRegistration(home, app) for app in APPS},
        listener=listener,
        http_origin=HTTP_ORIGIN,
        office_dist=office_dist,
        user_id_file=_user_id_file(home),
    )


def _wef_manifest(home: Path, app: str = "powerpoint") -> Path:
    return MacRegistration(home, app).path


def _user_id_file(home: Path) -> Path:
    return home / "data" / "office-addin" / "user-id"


def test_certificate_is_a_localhost_leaf_signed_by_a_ca_whose_key_is_not_kept(tmp_path: Path) -> None:
    files = TlsFiles(tmp_path / "tls")
    now = dt.datetime.now(dt.timezone.utc)
    certificate.generate(files, now)

    assert sorted(path.name for path in files.directory.iterdir()) == ["ca.crt", "localhost.crt", "localhost.key"]
    assert b"PRIVATE KEY" not in files.ca.read_bytes()
    ca, leaf = _load(files.ca), _load(files.cert)
    ca_constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert (ca_constraints.ca, ca_constraints.path_length) == (True, 0)
    assert ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage(
        [ExtendedKeyUsageOID.SERVER_AUTH]
    )
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["localhost"]
    assert {str(ip) for ip in names.get_values_for_type(x509.IPAddress)} == {"127.0.0.1", "::1"}
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc > dt.timedelta(days=824)
    assert _issued_by(leaf, ca)
    if os.name != "nt":
        assert files.key.stat().st_mode & 0o777 == 0o600


def _addin_id(manifest: str) -> str:
    return re.search(r"<Id>([^<]+)</Id>", manifest).group(1)


def test_install_registers_each_app_manifest_at_this_process_origin(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    keychain = FakeKeychain()
    addins = _mac_addins(home, office_dist, listeners, keychain)

    assert addins.install("office") == INSTALLED

    origin = listeners[0].origin
    user_id = _user_id_file(home).read_text(encoding="utf-8")
    assert user_id.startswith("office-")
    ids = set()
    for app in APPS:
        manifest = _wef_manifest(home, app).read_text(encoding="utf-8")
        pane = f"{origin}/office/taskpane.{app}.html?u={user_id}"
        assert f'<SourceLocation DefaultValue="{pane}" />' in manifest
        assert f'<bt:Url id="Taskpane.Url" DefaultValue="{pane}" />' in manifest
        assert f"<AppDomain>{origin}</AppDomain>" in manifest
        assert "localhost:3001" not in manifest
        source = (PANE_PACKAGE / f"manifest.{app}.xml").read_text(encoding="utf-8")
        assert _addin_id(manifest) == HOSTS[app].addin_id != _addin_id(source)
        ids.add(_addin_id(manifest))
    assert len(ids) == len(APPS)
    assert listeners[0].serving
    assert addins.status() == {"office": INSTALLED}


def test_install_again_changes_nothing_and_asks_for_no_trust(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    keychain = FakeKeychain()
    addins = _mac_addins(home, office_dist, listeners, keychain)
    addins.install("office")
    tls = TlsFiles(home / "data" / "office-addin" / "tls")
    ca_before = tls.ca.read_bytes()
    keychain.commands.clear()

    assert addins.install("office") == INSTALLED

    assert "add-trusted-cert" not in keychain.verbs()
    assert tls.ca.read_bytes() == ca_before


@pytest.mark.parametrize(
    ("missing", "installed"),
    [(("excel",), True), (APPS, False)],
    ids=["excel missing", "no app"],
)
def test_install_registers_the_apps_it_finds_and_reports_the_others_as_not_found(
    home: Path, office_dist: Path, listeners: list[TlsListener], missing: tuple[str, ...], installed: bool,
) -> None:
    for app in missing:
        (home / "Library" / "Containers" / HOSTS[app].mac_container).rmdir()
    keychain = FakeKeychain()
    addins = _mac_addins(home, office_dist, listeners, keychain)

    status = addins.install("office")

    apps = {app: "host_not_found" if app in missing else "installed" for app in APPS}
    assert status == SuiteStatus(installed=installed, problems=(), apps=apps)
    for app in missing:
        assert not (home / "Library" / "Containers" / HOSTS[app].mac_container).exists()
    assert bool(keychain.commands) == installed


def test_declined_trust_is_reported_and_nothing_is_registered(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    addins = _mac_addins(home, office_dist, listeners, FakeKeychain(accept=False))

    status = addins.install("office")

    assert status.installed is False
    assert "certificate_untrusted" in status.problems
    assert not _wef_manifest(home).exists()
    assert not listeners[0].serving


def test_install_renews_an_expiring_certificate_and_trusts_the_new_ca_before_untrusting_the_old(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    keychain = FakeKeychain()
    addins = _mac_addins(home, office_dist, listeners, keychain)
    tls = TlsFiles(home / "data" / "office-addin" / "tls")
    ten_days_left = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=certificate.VALID_DAYS - 10)
    certificate.generate(tls, ten_days_left)
    old_ca = _load(tls.ca)
    keychain.trusted[_sha1(old_ca)] = old_ca
    assert "certificate_expiring" in addins.status()["office"].problems
    keychain.commands.clear()

    assert addins.install("office") == INSTALLED

    verbs = keychain.verbs()
    assert verbs.index("add-trusted-cert") < verbs.index("remove-trusted-cert")
    assert _sha1(old_ca) not in keychain.trusted
    new_ca = _load(tls.ca)
    assert list(keychain.trusted) == [_sha1(new_ca)] and new_ca != old_ca


def test_ensure_registered_follows_a_port_change_and_leaves_a_current_manifest_alone(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    keychain = FakeKeychain()
    _mac_addins(home, office_dist, listeners, keychain).install("office")
    listeners[0].close()
    keychain.commands.clear()

    # The next launch got another port group.
    restarted = _mac_addins(home, office_dist, listeners, keychain)
    assert "stale_origin" in restarted.status()["office"].problems
    restarted.ensure_registered()
    manifest = _wef_manifest(home)
    assert f"{listeners[1].origin}/office/taskpane.powerpoint.html" in manifest.read_text(encoding="utf-8")
    assert restarted.status() == {"office": INSTALLED}

    os.utime(manifest, (1_000_000_000, 1_000_000_000))
    listeners[1].close()
    same_port = _mac_addins(home, office_dist, listeners, keychain, port=listeners[1].port)
    same_port.ensure_registered()
    assert manifest.stat().st_mtime == 1_000_000_000
    assert listeners[2].serving
    assert "add-trusted-cert" not in keychain.verbs()

    # A manifest without this install's user id, as one registered before the id existed.
    manifest.write_text(re.sub(r"\?u=[^\"]+", "", manifest.read_text(encoding="utf-8")), encoding="utf-8")
    assert "stale_origin" in same_port.status()["office"].problems
    same_port.ensure_registered()
    assert same_port.status() == {"office": INSTALLED}


def test_remove_deletes_only_our_manifest_and_releases_the_certificate_but_keeps_the_user_id(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    keychain = FakeKeychain()
    addins = _mac_addins(home, office_dist, listeners, keychain)
    addins.install("office")
    other = _wef_manifest(home).with_name("someone-else.xml")
    other.write_text("<OfficeApp/>", encoding="utf-8")

    assert addins.remove("office") == NOT_INSTALLED

    assert not any(_wef_manifest(home, app).exists() for app in APPS)
    assert other.read_text(encoding="utf-8") == "<OfficeApp/>"
    assert keychain.trusted == {}
    assert not TlsFiles(home / "data" / "office-addin" / "tls").exist()
    assert not listeners[0].serving

    user_id = _user_id_file(home).read_text(encoding="utf-8")
    assert addins.install("office") == INSTALLED
    assert f"?u={user_id}" in _wef_manifest(home).read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as a normal user")
def test_unwritable_sideload_folder_reports_permission_denied(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    wef = _wef_manifest(home).parent
    wef.mkdir(parents=True)
    wef.chmod(0o500)
    try:
        status = _mac_addins(home, office_dist, listeners, FakeKeychain()).install("office")
    finally:
        wef.chmod(0o700)

    assert status.installed is False
    assert "permission_denied" in status.problems


def test_an_app_older_than_its_manifest_requires_is_left_out_with_the_version_to_update_to(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    applications = home.parent / "Applications"
    for app, version in {"powerpoint": "16.95.2", "word": "16.76", "excel": "16.104"}.items():
        info = applications / HOSTS[app].mac_bundle / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
    registrations = {app: MacRegistration(home, app, applications) for app in APPS}
    addins = _mac_addins(home, office_dist, listeners, FakeKeychain(), registrations=registrations)

    status = addins.install("office")

    assert status == SuiteStatus(
        installed=True, problems=(), required={"powerpoint": "16.96"},
        apps={"powerpoint": "host_outdated", "word": "installed", "excel": "installed"},
    )
    assert not _wef_manifest(home).exists()

    # A version that cannot be read is unknown, and installs.
    (applications / HOSTS["powerpoint"].mac_bundle / "Contents" / "Info.plist").write_bytes(b"not a plist")
    assert addins.install("office") == INSTALLED


def test_windows_leaves_out_the_apps_its_office_build_is_too_old_for(
    tmp_path: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    registry = FakeRegistry()
    data = tmp_path / "data" / "office-addin"
    listener = TlsListener(_free_port(), partial(SimpleHTTPRequestHandler, directory=str(office_dist.parent)))
    listeners.append(listener)
    addins = OfficeAddins(
        tls=TlsFiles(data / "tls"),
        trust=WindowsTrustStore(runner=FakeCertutil()),
        registrations={app: WindowsRegistration(data, registry, app, lambda: "16.0.17928.20114") for app in APPS},
        listener=listener,
        http_origin=HTTP_ORIGIN,
        office_dist=office_dist,
        user_id_file=data / "user-id",
    )

    status = addins.install("office")

    assert status.apps == {"powerpoint": "host_outdated", "word": "installed", "excel": "installed"}
    assert status.required == {"powerpoint": "2504"}
    assert registry.get(WindowsRegistration.KEY, HOSTS["powerpoint"].addin_id) is None


def test_windows_registers_our_manifest_path_under_our_id(
    tmp_path: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    registry = FakeRegistry()
    registry.set(WindowsRegistration.KEY, "another-add-in", r"C:\elsewhere\manifest.xml")
    certutil = FakeCertutil()
    data = tmp_path / "data" / "office-addin"
    listener = TlsListener(_free_port(), partial(SimpleHTTPRequestHandler, directory=str(office_dist.parent)))
    listeners.append(listener)
    addins = OfficeAddins(
        tls=TlsFiles(tmp_path / "data" / "office-addin" / "tls"),
        trust=WindowsTrustStore(runner=certutil),
        registrations={app: WindowsRegistration(data, registry, app) for app in APPS},
        listener=listener,
        http_origin=HTTP_ORIGIN,
        office_dist=office_dist,
        user_id_file=tmp_path / "data" / "office-addin" / "user-id",
    )

    assert addins.install("office") == INSTALLED
    for app in APPS:
        manifest_path = data / f"manifest.{app}.xml"
        assert registry.get(WindowsRegistration.KEY, HOSTS[app].addin_id) == str(manifest_path)
        assert f"{listener.origin}/office/taskpane.{app}.html" in manifest_path.read_text(encoding="utf-8")
    assert len(certutil.root) == 1

    assert addins.remove("office") == NOT_INSTALLED
    assert registry.values == {(WindowsRegistration.KEY, "another-add-in"): r"C:\elsewhere\manifest.xml"}
    assert not list(data.glob("manifest.*.xml"))
    assert certutil.root == set()


def test_mac_trust_check_uses_the_basic_policy(tmp_path: Path) -> None:
    keychain = FakeKeychain()
    files = TlsFiles(tmp_path / "tls")
    certificate.generate(files, dt.datetime.now(dt.timezone.utc))

    MacTrustStore(tmp_path, runner=keychain).is_trusted(files)

    assert keychain.commands == [["security", "verify-cert", "-c", str(files.cert), "-p", "basic"]]


def test_status_reads_the_registration_fresh(home: Path, office_dist: Path, listeners: list[TlsListener]) -> None:
    addins = _mac_addins(home, office_dist, listeners, FakeKeychain())
    addins.install("office")

    _wef_manifest(home).unlink()

    assert addins.status()["office"].installed is False


def test_remove_keeps_everything_installed_while_the_system_keeps_trusting_the_certificate(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    keychain = FakeKeychain()
    addins = _mac_addins(home, office_dist, listeners, keychain)
    addins.install("office")
    tls = TlsFiles(home / "data" / "office-addin" / "tls")

    keychain.accept = False
    assert addins.remove("office") == INSTALLED
    assert tls.exist() and _wef_manifest(home).exists() and listeners[0].serving
    assert "delete-certificate" not in keychain.verbs()

    keychain.accept = True
    assert addins.remove("office") == NOT_INSTALLED
    assert keychain.trusted == {} and not tls.exist()


def test_uninstall_cleanup_fails_while_the_system_keeps_trusting_the_certificate(
    home: Path, office_dist: Path, listeners: list[TlsListener], monkeypatch: pytest.MonkeyPatch,
) -> None:
    keychain = FakeKeychain()
    _mac_addins(home, office_dist, listeners, keychain).install("office")
    tls = TlsFiles(home / "data" / "office-addin" / "tls")
    monkeypatch.setattr(office_addins, "_machine", lambda _workspace: (tls, MacTrustStore(home, runner=keychain), {}))

    keychain.accept = False
    assert office_addins.remove_all(home / "data") is False
    assert tls.exist()


@pytest.mark.parametrize("damage", ["deleted", "truncated"])
def test_a_registered_add_in_without_a_usable_certificate_needs_repair(
    home: Path, office_dist: Path, listeners: list[TlsListener], damage: str,
) -> None:
    addins = _mac_addins(home, office_dist, listeners, FakeKeychain())
    addins.install("office")
    tls = TlsFiles(home / "data" / "office-addin" / "tls")
    if damage == "deleted":
        tls.delete()
    else:
        tls.ca.write_bytes(tls.ca.read_bytes()[:100])

    status = addins.status()["office"]
    assert status.installed and "certificate_untrusted" in status.problems

    assert addins.install("office") == INSTALLED


def test_an_unreadable_user_id_is_replaced(home: Path, office_dist: Path, listeners: list[TlsListener]) -> None:
    user_id_file = _user_id_file(home)
    user_id_file.parent.mkdir(parents=True)
    user_id_file.write_bytes(b"")

    assert _mac_addins(home, office_dist, listeners, FakeKeychain()).install("office") == INSTALLED

    user_id = user_id_file.read_text(encoding="utf-8")
    assert re.fullmatch(r"office-[0-9a-f-]{36}", user_id)
    assert f"?u={user_id}" in _wef_manifest(home).read_text(encoding="utf-8")


class _RefusingRegistration(MacRegistration):
    """An app's registration the system does not let the desktop read or change."""

    def host_found(self) -> bool:
        raise PermissionError("Operation not permitted")

    def read(self) -> str | None:
        raise PermissionError("Operation not permitted")

    def write(self, manifest: str) -> None:
        raise PermissionError("Operation not permitted")

    def delete(self) -> None:
        raise PermissionError("Operation not permitted")


def test_a_refused_app_is_reported_and_the_others_install_and_remove(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    keychain = FakeKeychain()
    registrations = {app: MacRegistration(home, app) for app in APPS}
    registrations["word"] = _RefusingRegistration(home, "word")
    addins = _mac_addins(home, office_dist, listeners, keychain, registrations=registrations)
    refused = {"powerpoint": "installed", "word": "not_installed", "excel": "installed"}

    assert addins.install("office") == SuiteStatus(installed=False, problems=("permission_denied",), apps=refused)
    assert addins.status()["office"] == SuiteStatus(installed=False, problems=("permission_denied",), apps=refused)
    addins.ensure_registered()

    status = addins.remove("office")

    assert status.apps == {"powerpoint": "not_installed", "word": "not_installed", "excel": "not_installed"}
    assert "permission_denied" in status.problems
    assert keychain.trusted == {}
    assert not TlsFiles(home / "data" / "office-addin" / "tls").exist()
    assert not listeners[0].serving


def test_an_unanswered_trust_prompt_is_reported(
    home: Path, office_dist: Path, listeners: list[TlsListener], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(certificate, "PROMPT_TIMEOUT_SECONDS", 0.2)
    keychain = FakeKeychain()

    def unanswered(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if args[1] == "add-trusted-cert":
            return certificate.run_command([sys.executable, "-c", "import time; time.sleep(30)"])
        return keychain(args)

    status = _mac_addins(home, office_dist, listeners, unanswered).install("office")

    assert status.installed is False
    assert "certificate_untrusted" in status.problems


# As WPS for Mac writes it, with another add-in WPS loads.
WPS_LIST = """<?xml version="1.0" encoding="UTF-8"?>
<jsplugins>
    <jspluginonline enable="enable_dev" type="wps" name="排版助手" install="null" url="http://127.0.0.1:3889/typeset/"/>
  </jsplugins>
"""
# An empty list, as WPS writes it.
WPS_EMPTY_LIST = '<?xml version="1.0" encoding="UTF-8"?>\n<jsplugins/>\n'
WPS_INSTALLED = SuiteStatus(installed=True, problems=(), apps=dict.fromkeys(WPS_APPS, "installed"))


def _wps_list(home: Path, edition: str) -> Path:
    return home / WPS_MAC_EDITIONS[edition].folder / WPS_MAC_EDITIONS[edition].publish


def _wps_addins(
    home: Path, office_dist: Path, listeners: list[TlsListener], keychain: Runner, editions: Iterable[str],
) -> OfficeAddins:
    for edition in editions:
        (home / WPS_MAC_EDITIONS[edition].folder).mkdir(parents=True)
    registrations = {app: MacRegistration(home, app) for app in APPS}
    registrations.update({app: WpsRegistration(home, WPS_MAC_EDITIONS, app) for app in WPS_APPS})
    return _mac_addins(home, office_dist, listeners, keychain, registrations=registrations)


def test_wps_install_and_remove_change_only_our_entries_in_every_edition_and_need_no_certificate(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    keychain = FakeKeychain()
    addins = _wps_addins(home, office_dist, listeners, keychain, WPS_MAC_EDITIONS)
    international, domestic = _wps_list(home, "international"), _wps_list(home, "domestic")
    for publish, text in ((international, WPS_LIST), (domestic, WPS_EMPTY_LIST)):
        publish.parent.mkdir(parents=True)
        publish.write_text(text, encoding="utf-8")

    status = addins.install("wps")

    assert status == WPS_INSTALLED
    for publish in (international, domestic):
        text = publish.read_text(encoding="utf-8")
        for app_type in ("wpp", "wps", "et"):
            entry = (
                f'<jspluginonline enable="enable_dev" type="{app_type}" name="WorkSwarm" install="null"'
                f' url="{HTTP_ORIGIN}/office/wps/{app_type}/"/>'
            )
            assert text.count(entry) == 1
    assert "排版助手" in international.read_text(encoding="utf-8")
    assert keychain.commands == [] and not listeners[0].serving
    assert addins.status() == {"office": NOT_INSTALLED, "wps": status}

    assert addins.install("office") == INSTALLED
    assert addins.remove("wps").installed is False
    assert international.read_text(encoding="utf-8") == WPS_LIST
    assert domestic.read_text(encoding="utf-8") == '<?xml version="1.0" encoding="UTF-8"?>\n<jsplugins>\n</jsplugins>\n'
    assert addins.status()["office"] == INSTALLED and keychain.trusted


def test_ensure_registered_points_wps_at_the_new_port_without_a_certificate(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    addins = _wps_addins(home, office_dist, listeners, FakeKeychain(), ["domestic"])
    addins.install("wps")
    publish = _wps_list(home, "domestic")
    moved = publish.read_text(encoding="utf-8").replace(HTTP_ORIGIN, "http://127.0.0.1:6173")
    publish.write_text(moved, encoding="utf-8")
    # The other edition ran for the first time since the install, so it holds no entry of ours.
    (home / WPS_MAC_EDITIONS["international"].folder).mkdir()
    assert addins.status()["wps"] == SuiteStatus(installed=True, problems=("stale_origin",), apps=WPS_INSTALLED.apps)

    addins.ensure_registered()

    assert addins.status()["wps"] == WPS_INSTALLED
    assert "6173" not in publish.read_text(encoding="utf-8")
    assert not _wps_list(home, "international").exists()


def test_wps_status_does_not_need_the_office_user_id(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    addins = _wps_addins(home, office_dist, listeners, FakeKeychain(), ["domestic"])
    addins.install("wps")
    _user_id_file(home).unlink()

    assert addins.status()["wps"] == WPS_INSTALLED


def test_a_broken_word_manifest_leaves_wps_status_and_remove_working(
    home: Path, office_dist: Path, listeners: list[TlsListener],
) -> None:
    addins = _wps_addins(home, office_dist, listeners, FakeKeychain(), ["domestic"])
    addins.install("wps")
    _wef_manifest(home, "word").parent.mkdir(parents=True)
    _wef_manifest(home, "word").write_bytes(b"\xff\xfe not utf-8")

    status = addins.status()
    assert status["wps"] == WPS_INSTALLED
    assert status["office"].problems == ("permission_denied",)
    assert addins.remove("wps").installed is False
    assert "WorkSwarm" not in _wps_list(home, "domestic").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "data", [b'<?xml version="1.0" encoding="UTF-8"?>\n<jsplugins>\xff</jsplugins>', b'<?xml encoding="nope"?>'],
)
def test_an_undecodable_wps_list_fails_only_wps_and_office_still_installs_and_removes(
    home: Path, office_dist: Path, listeners: list[TlsListener], data: bytes,
) -> None:
    addins = _wps_addins(home, office_dist, listeners, FakeKeychain(), ["domestic"])
    _wps_list(home, "domestic").parent.mkdir(parents=True)
    _wps_list(home, "domestic").write_bytes(data)

    assert addins.install("wps").problems == ("permission_denied",)
    assert _wps_list(home, "domestic").read_bytes() == data
    assert addins.status()["office"] == NOT_INSTALLED
    assert addins.install("office") == INSTALLED
    assert addins.remove("office") == NOT_INSTALLED


def test_windows_offers_wps_and_installs_repairs_and_uninstalls_through_its_list(
    tmp_path: Path, office_dist: Path, listeners: list[TlsListener], monkeypatch: pytest.MonkeyPatch,
) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    workspace = tmp_path / "data"
    monkeypatch.setattr(office_addins, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(office_addins, "WinRegistry", FakeRegistry)
    monkeypatch.setenv("APPDATA", str(appdata))

    def this_machine() -> OfficeAddins:
        listener = TlsListener(_free_port(), partial(SimpleHTTPRequestHandler, directory=str(office_dist.parent)))
        listeners.append(listener)
        return OfficeAddins.for_this_machine(workspace, listener, HTTP_ORIGIN, office_dist)

    assert set(this_machine().status()["wps"].apps.values()) == {"host_not_found"}

    publish = appdata / "kingsoft" / "wps" / "jsaddons" / "publish.xml"
    assert {appdata / edition.folder / edition.publish for edition in WPS_WINDOWS_EDITIONS.values()} == {publish}
    publish.parent.mkdir(parents=True)
    publish.write_text(WPS_LIST, encoding="utf-8")
    addins = this_machine()

    assert addins.install("wps") == WPS_INSTALLED
    assert publish.read_text(encoding="utf-8").count(f'name="WorkSwarm" install="null" url="{HTTP_ORIGIN}') == 3
    assert not (workspace / "office-addin" / "tls").exists() and not listeners[-1].serving
    moved = publish.read_text(encoding="utf-8").replace(HTTP_ORIGIN, "http://127.0.0.1:6173")
    publish.write_text(moved, encoding="utf-8")
    assert addins.status()["wps"].problems == ("stale_origin",)
    addins.ensure_registered()
    assert addins.status()["wps"] == WPS_INSTALLED

    assert office_addins.remove_all(workspace) is True
    assert publish.read_text(encoding="utf-8") == WPS_LIST
