"""HTTP avatar references preserve bytes without inflating catalog messages."""

import base64
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from jiuwenswarm.common.file_download_token import WebFileDownloadManager
from jiuwenswarm.server.runtime import package_avatars


@pytest.fixture
def avatar_workspace(extension_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    manager = WebFileDownloadManager(secret="avatar-test-secret")
    monkeypatch.setattr(WebFileDownloadManager, "_instance", manager)
    return extension_workspace


def test_file_and_inline_avatar_share_cached_http_asset(avatar_workspace: Path, tmp_path: Path) -> None:
    # Resource packages can live outside the user's downloadable workspace.
    package = tmp_path / "resources" / "group"
    package.mkdir(parents=True)
    image = b"\x89PNG\r\n\x1a\nimage-content"
    source = package / "avatar.png"
    source.write_bytes(image)
    file_url = package_avatars.resolve_package_avatar_url("avatar.png", package_dir=package)
    inline = "data:image/png;base64," + base64.b64encode(image).decode("ascii")

    inline_url = package_avatars.resolve_package_avatar_url(inline)
    query = parse_qs(urlsplit(file_url).query)
    assert query["inline"] == ["1"]
    claims = WebFileDownloadManager.get_instance().validate_token(query["token"][0])
    assert claims is not None
    cached = Path(claims["path"])
    inline_token = parse_qs(urlsplit(inline_url).query)["token"][0]
    inline_claims = WebFileDownloadManager.get_instance().validate_token(inline_token)
    assert inline_claims is not None
    assert inline_claims["path"] == str(cached)
    assert cached.is_relative_to(avatar_workspace)
    source.unlink()
    assert cached.read_bytes() == image


@pytest.mark.parametrize("raw", [
    "../outside.png", "/tmp/outside.png", "C:\\outside.png", "missing.png",
    "data:image/png;base64,invalid!", "data:image/png,unencoded", "javascript:alert(1)",
    "data:image/svg+xml,<svg/>", "missing.svg", "avatar.gif.exe",
])
def test_invalid_avatar_is_omitted(avatar_workspace: Path, tmp_path: Path, raw: str) -> None:
    assert package_avatars.resolve_package_avatar_url(raw, package_dir=tmp_path) == ""


def test_signed_avatar_url_outlives_a_long_session(avatar_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    url = package_avatars.resolve_package_avatar_url("data:image/png;base64,eA==")
    token = parse_qs(urlsplit(url).query)["token"][0]
    later = time.time() + 24 * 3600
    monkeypatch.setattr(time, "time", lambda: later)
    assert WebFileDownloadManager.get_instance().validate_token(token) is not None


def test_svg_avatar_stays_inline_and_is_never_served_from_our_origin(avatar_workspace: Path, tmp_path: Path) -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    (tmp_path / "avatar.svg").write_bytes(svg)
    expected = "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")

    assert package_avatars.resolve_package_avatar_url("avatar.svg", package_dir=tmp_path) == expected
    assert package_avatars.resolve_package_avatar_url(expected) == expected
    assert not (avatar_workspace / ".cache" / "package-avatars").exists()


def test_oversized_svg_avatar_is_omitted(avatar_workspace: Path, tmp_path: Path) -> None:
    (tmp_path / "avatar.svg").write_bytes(b"<svg>" + b" " * (64 * 1024))
    assert package_avatars.resolve_package_avatar_url("avatar.svg", package_dir=tmp_path) == ""


def test_symlink_cannot_escape_package(avatar_workspace: Path, tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private")
    (package / "avatar.png").symlink_to(outside)

    assert package_avatars.resolve_package_avatar_url("avatar.png", package_dir=package) == ""


def test_cache_symlink_cannot_write_outside_workspace(avatar_workspace: Path, tmp_path: Path) -> None:
    avatar_workspace.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (avatar_workspace / ".cache").symlink_to(outside, target_is_directory=True)

    assert package_avatars.resolve_package_avatar_url("data:image/png;base64,eA==") == ""
    assert list(outside.iterdir()) == []


def test_remote_avatar_url_is_preserved(avatar_workspace: Path) -> None:
    url = "https://example.test/avatar.png"
    assert package_avatars.resolve_package_avatar_url(url) == url


def test_avatar_caches_are_isolated_by_tenant(avatar_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: True)
    monkeypatch.setattr("jiuwenswarm.enterprise.tenant.tenant_paths.is_enterprise", lambda: True)
    claims_by_tenant = []
    for tenant in ("tenant-a", "tenant-b"):
        with TenantContext.scoped(tenant):
            url = package_avatars.resolve_package_avatar_url("data:image/png;base64,eA==")
            token = parse_qs(urlsplit(url).query)["token"][0]
            claims = WebFileDownloadManager.get_instance().validate_token(token)
            assert claims is not None
            claims_by_tenant.append(claims)
    assert claims_by_tenant[0]["path"] != claims_by_tenant[1]["path"]
    assert Path(claims_by_tenant[0]["path"]).read_bytes() == b"x"
    assert Path(claims_by_tenant[1]["path"]).read_bytes() == b"x"
