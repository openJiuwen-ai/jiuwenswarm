# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The localhost certificate behind the task panes' HTTPS origin.

Office loads a task pane only from an HTTPS origin the system trusts. The
desktop makes a CA, signs one ``localhost`` certificate with it, and asks the
system to trust the CA. The CA's private key is never written down, so the
trusted CA can sign nothing else.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VALID_DAYS = 825  # the longest server certificate lifetime macOS accepts
RENEW_WITHIN_DAYS = 30
CA_NAME = "WorkSwarm Office Add-in Local CA"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False, timeout=300)


@dataclass(frozen=True)
class TlsFiles:
    directory: Path

    @property
    def ca(self) -> Path:
        return self.directory / "ca.crt"

    @property
    def cert(self) -> Path:
        return self.directory / "localhost.crt"

    @property
    def key(self) -> Path:
        return self.directory / "localhost.key"

    def exist(self) -> bool:
        return self.ca.is_file() and self.cert.is_file() and self.key.is_file()

    def expiring(self, now: dt.datetime) -> bool:
        leaf = x509.load_pem_x509_certificate(self.cert.read_bytes())
        return leaf.not_valid_after_utc - now < dt.timedelta(days=RENEW_WITHIN_DAYS)

    def delete(self) -> None:
        for path in (self.cert, self.key, self.ca):
            path.unlink(missing_ok=True)


def generate(files: TlsFiles, now: dt.datetime) -> None:
    """Write a new CA and a ``localhost`` certificate it signed; the CA key stays in memory."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    ca_cert = (
        _builder(ca_name, ca_name, ca_key.public_key(), now)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(cert_sign=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    leaf_cert = (
        _builder(leaf_name, ca_name, leaf_key.public_key(), now)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(cert_sign=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.IPAddress(ipaddress.ip_address("::1")),
        ]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    files.directory.mkdir(parents=True, exist_ok=True)
    with open(files.key, "wb", opener=_owner_only) as handle:
        handle.write(leaf_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption(),
        ))
    files.cert.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    # Written last: a set interrupted before this line lacks ca.crt, so it counts as absent and is made again.
    files.ca.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))


def _owner_only(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _builder(subject: x509.Name, issuer: x509.Name, public_key, now: dt.datetime) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
    )


def _key_usage(*, cert_sign: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not cert_sign, key_encipherment=not cert_sign, content_commitment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=cert_sign, crl_sign=cert_sign,
        encipher_only=False, decipher_only=False,
    )


def _sha1(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.fingerprint(hashes.SHA1()).hex().upper()  # noqa: S324 - the store's identifier, not a signature


class TrustStore(Protocol):
    def is_trusted(self, files: TlsFiles) -> bool:
        ...

    def trust(self, files: TlsFiles) -> bool:
        ...

    def untrust(self, files: TlsFiles) -> None:
        ...


class MacTrustStore:
    """The login keychain, through ``security``; adding a trusted root asks for the user's password."""

    def __init__(self, home: Path, runner: Runner = run_command) -> None:
        self._keychain = str(home / "Library" / "Keychains" / "login.keychain-db")
        self._run = runner

    def is_trusted(self, files: TlsFiles) -> bool:
        # ``-p ssl`` also demands certificate transparency, which no local certificate has.
        return self._run(["security", "verify-cert", "-c", str(files.cert), "-p", "basic"]).returncode == 0

    def trust(self, files: TlsFiles) -> bool:
        command = ["security", "add-trusted-cert", "-r", "trustRoot", "-k", self._keychain, str(files.ca)]
        return self._run(command).returncode == 0

    def untrust(self, files: TlsFiles) -> None:
        self._run(["security", "remove-trusted-cert", str(files.ca)])
        self._run(["security", "delete-certificate", "-Z", _sha1(files.ca), self._keychain])


class WindowsTrustStore:
    """The current user's Root store, through ``certutil``; adding to it shows Windows' own confirmation."""

    def __init__(self, runner: Runner = run_command) -> None:
        self._run = runner

    def is_trusted(self, files: TlsFiles) -> bool:
        return self._run(["certutil", "-user", "-verifystore", "Root", _sha1(files.ca)]).returncode == 0

    def trust(self, files: TlsFiles) -> bool:
        return self._run(["certutil", "-user", "-addstore", "Root", str(files.ca)]).returncode == 0

    def untrust(self, files: TlsFiles) -> None:
        self._run(["certutil", "-user", "-delstore", "Root", _sha1(files.ca)])
