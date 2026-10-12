# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bundled ripgrep: pinned download, checksum refusals, offline copy, build wiring."""

from __future__ import annotations

import hashlib
import io
import re
import runpy
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FETCH = runpy.run_path(str(ROOT / "scripts" / "fetch_ripgrep.py"))
Asset = FETCH["Asset"]
fetch_platform = FETCH["fetch_platform"]

FAKE_RG = b"#!/bin/sh\necho 'ripgrep 15.2.0'\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar_gz(binary: bytes, name: str = "rg") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(f"ripgrep-15.2.0-test/{name}")
        info.size = len(binary)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(binary))
        lic = b"MIT"
        info = tarfile.TarInfo("ripgrep-15.2.0-test/LICENSE-MIT")
        info.size = len(lic)
        tf.addfile(info, io.BytesIO(lic))
    return buf.getvalue()


def _zip(binary: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ripgrep-15.2.0-test/rg.exe", binary)
    return buf.getvalue()


def _table(
    archive: bytes, binary: bytes = FAKE_RG, name: str = "rg", kind: str = "tar.gz"
):
    return {
        "test-plat": Asset(f"ripgrep-test.{kind}", name, _sha(archive), _sha(binary))
    }


class _Downloads:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return self.payload


def test_download_is_verified_extracted_and_executable(tmp_path: Path) -> None:
    archive = _tar_gz(FAKE_RG)
    dl = _Downloads(archive)

    path = fetch_platform("test-plat", tmp_path, downloader=dl, assets=_table(archive))

    assert path == tmp_path / "test-plat" / "rg"
    assert path.read_bytes() == FAKE_RG
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o111
    assert dl.urls == [f"{FETCH['BASE_URL']}/ripgrep-test.tar.gz"]


def test_zip_assets_extract_rg_exe(tmp_path: Path) -> None:
    archive = _zip(FAKE_RG)
    table = _table(archive, name="rg.exe", kind="zip")

    path = fetch_platform(
        "test-plat", tmp_path, downloader=_Downloads(archive), assets=table
    )

    assert path.name == "rg.exe" and path.read_bytes() == FAKE_RG


def test_archive_with_wrong_checksum_is_refused_and_nothing_written(
    tmp_path: Path,
) -> None:
    archive = _tar_gz(FAKE_RG)
    table = _table(_tar_gz(b"something else"))  # pins a different archive

    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetch_platform(
            "test-plat", tmp_path, downloader=_Downloads(archive), assets=table
        )
    assert not (tmp_path / "test-plat").exists()


def test_binary_with_wrong_checksum_is_refused(tmp_path: Path) -> None:
    archive = _tar_gz(b"tampered rg")
    table = {
        "test-plat": Asset("ripgrep-test.tar.gz", "rg", _sha(archive), _sha(FAKE_RG))
    }

    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetch_platform(
            "test-plat", tmp_path, downloader=_Downloads(archive), assets=table
        )
    assert not (tmp_path / "test-plat" / "rg").exists()


def test_offline_copy_must_match_the_pinned_binary(tmp_path: Path) -> None:
    table = _table(_tar_gz(FAKE_RG))
    good = tmp_path / "vendor" / "test-plat" / "rg"
    good.parent.mkdir(parents=True)
    good.write_bytes(FAKE_RG)

    def no_network(url: str) -> bytes:
        raise AssertionError("offline copy must not download")

    path = fetch_platform(
        "test-plat",
        tmp_path / "out",
        from_dir=tmp_path / "vendor",
        downloader=no_network,
        assets=table,
    )
    assert path.read_bytes() == FAKE_RG

    good.write_bytes(b"not the pinned binary")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetch_platform(
            "test-plat",
            tmp_path / "out2",
            from_dir=tmp_path / "vendor",
            downloader=no_network,
            assets=table,
        )


def test_verified_copy_is_reused_and_a_tampered_one_replaced(tmp_path: Path) -> None:
    archive = _tar_gz(FAKE_RG)
    table = _table(archive)
    dl = _Downloads(archive)
    path = fetch_platform("test-plat", tmp_path, downloader=dl, assets=table)

    fetch_platform("test-plat", tmp_path, downloader=dl, assets=table)
    assert len(dl.urls) == 1  # reused

    path.write_bytes(b"tampered")
    fetch_platform("test-plat", tmp_path, downloader=dl, assets=table)
    assert len(dl.urls) == 2 and path.read_bytes() == FAKE_RG


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS quarantine flag")
def test_quarantine_flag_is_removed(tmp_path: Path) -> None:
    import subprocess

    archive = _tar_gz(FAKE_RG)
    table = _table(archive)
    path = fetch_platform(
        "test-plat", tmp_path, downloader=_Downloads(archive), assets=table
    )
    subprocess.run(
        ["xattr", "-w", "com.apple.quarantine", "0081;0;test;", str(path)], check=True
    )

    fetch_platform(
        "test-plat", tmp_path, downloader=_Downloads(archive), assets=table
    )  # reuse path
    attrs = subprocess.run(
        ["xattr", str(path)], capture_output=True, text=True, check=False
    ).stdout
    assert "com.apple.quarantine" not in attrs


def test_pinned_table_covers_every_desktop_platform() -> None:
    assets = FETCH["ASSETS"]
    assert set(assets) == {
        "darwin-arm64",
        "darwin-x86_64",
        "linux-x86_64",
        "linux-aarch64",
        "windows-x86_64",
    }
    for key, asset in assets.items():
        assert re.fullmatch(r"[0-9a-f]{64}", asset.archive_sha256), key
        assert re.fullmatch(r"[0-9a-f]{64}", asset.binary_sha256), key
        assert asset.archive.startswith(f"ripgrep-{FETCH['RG_VERSION']}-")
        assert asset.binary == ("rg.exe" if key.startswith("windows") else "rg")


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-x86_64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Windows", "AMD64", "windows-x86_64"),
        ("Windows", "ARM64", None),
        ("FreeBSD", "x86_64", None),
    ],
)
def test_platform_key(system: str, machine: str, key: str | None) -> None:
    assert FETCH["platform_key"](system, machine) == key


def test_build_hook_honours_the_offline_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = FETCH["platform_key"]()
    if key is None:
        pytest.skip("no ripgrep build for this host")
    asset = FETCH["ASSETS"][key]
    vendor = tmp_path / "vendor" / key
    vendor.mkdir(parents=True)
    (vendor / asset.binary).write_bytes(b"wrong")
    monkeypatch.setenv(FETCH["FROM_ENV"], str(tmp_path / "vendor"))

    with pytest.raises(ValueError, match="sha256 mismatch"):
        FETCH["ensure_platform_binary"](tmp_path / "project")


# ── build wiring ────────────────────────────────────────────────────────────


def test_spec_bundles_ripgrep_off_path_and_outside_upx() -> None:
    spec = (ROOT / "scripts" / "jiuwenswarm.spec").read_text(encoding="utf-8")

    assert '"fetch_ripgrep.py"' in spec and "ensure_platform_binary" in spec
    assert (
        '_bundled_binaries.append((str(_rg_binary), os.path.join("vendor", "ripgrep")))'
        in spec
    )
    assert 'upx_exclude=["rg", "rg.exe"]' in spec
    assert "JIUWENSWARM_SKIP_RG_BUNDLE" in spec


def test_every_desktop_build_runs_the_frozen_ripgrep_verifier() -> None:
    for name in (
        "build-macos.sh",
        "build-exe.ps1",
        "build-exe.bat",
        "build-electron-exe.sh",
        "build-electron-exe.ps1",
    ):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "verify_ripgrep_bundle.py" in source, name


def test_frozen_entry_runs_the_self_check_and_agent_server_reports_it() -> None:
    entry = (ROOT / "scripts" / "jiuwenswarm_exe_entry.py").read_text(encoding="utf-8")
    server = (ROOT / "jiuwenswarm" / "server" / "app_agentserver.py").read_text(
        encoding="utf-8"
    )

    assert "configure_bundled_ripgrep()" in entry
    assert "report_bundled_ripgrep_status(logger)" in server


def test_verifier_version_matches_the_fetch_pin_and_refuses_outside_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = runpy.run_path(str(ROOT / "scripts" / "verify_ripgrep_bundle.py"))
    assert verifier["EXPECTED_VERSION"] == FETCH["RG_VERSION"]

    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(RuntimeError, match="frozen executable"):
        verifier["verify_ripgrep_bundle"]()


def test_license_notice_lists_ripgrep() -> None:
    notice = (ROOT / "OPEN_SOURCE_SOFTWARE_NOTICE.md").read_text(encoding="utf-8")
    assert "### Bundled ripgrep" in notice
    assert f"| ripgrep | {FETCH['RG_VERSION']} |" in notice
    assert "Copyright (c) 2015 Andrew Gallant" in notice
