"""企业版 skills.import_upload 上传资格守卫。"""

from __future__ import annotations

import pytest

from gateway.enterprise.capabilities.skill_upload import guard as guard_mod
from gateway.enterprise.capabilities.skill_upload import client as client_mod

pytestmark = pytest.mark.unit


def _patch_enterprise(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    import gateway.enterprise as enterprise_pkg

    monkeypatch.setattr(enterprise_pkg, "is_enterprise", lambda: enabled)


@pytest.mark.asyncio
async def test_personal_edition_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch, False)
    called = False

    async def _boom(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not query manager in personal edition")

    monkeypatch.setattr(client_mod, "query_upload_permission", _boom)
    await guard_mod.assert_skill_upload_permission(
        authorization=None,
        jiuwenclaw_id=None,
        method="skills.import_upload",
        params={"path": "/tmp/a.zip"},
    )
    assert called is False


@pytest.mark.asyncio
async def test_download_token_does_not_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch, True)
    calls: list[dict] = []

    async def _allowed(**kwargs):
        calls.append(kwargs)
        return {"allowed": True, "source": "user"}

    monkeypatch.setattr(client_mod, "query_upload_permission", _allowed)
    await guard_mod.assert_skill_upload_permission(
        authorization="Bearer t",
        jiuwenclaw_id="inst-1",
        method="skills.import_upload",
        params={"download_token": "tok", "path": "/tmp/a.zip"},
    )
    assert calls, "import_upload must still query permission with download_token present"


@pytest.mark.asyncio
async def test_other_methods_are_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch, True)
    await guard_mod.assert_skill_upload_permission(
        authorization=None,
        jiuwenclaw_id=None,
        method="skills.list",
        params={},
    )


@pytest.mark.asyncio
async def test_missing_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch, True)
    with pytest.raises(client_mod.SkillUploadPermissionError) as exc:
        await guard_mod.assert_skill_upload_permission(
            authorization="Bearer t",
            jiuwenclaw_id=None,
            method="skills.import_upload",
            params={"path": "/tmp/a.zip"},
        )
    assert exc.value.code == client_mod.CHECK_UNAVAILABLE

    with pytest.raises(client_mod.SkillUploadPermissionError) as exc2:
        await guard_mod.assert_skill_upload_permission(
            authorization=None,
            jiuwenclaw_id="inst-1",
            method="skills.import_upload",
            params={"path": "/tmp/a.zip"},
        )
    assert exc2.value.code == client_mod.CHECK_UNAVAILABLE


@pytest.mark.asyncio
async def test_allowed_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch, True)

    async def _allowed(**_kwargs):
        return {"allowed": True, "source": "user"}

    monkeypatch.setattr(client_mod, "query_upload_permission", _allowed)
    await guard_mod.assert_skill_upload_permission(
        authorization="Bearer t",
        jiuwenclaw_id="inst-1",
        method="skills.import_upload",
        params={"path": "/tmp/a.zip"},
    )


@pytest.mark.asyncio
async def test_denied_raises_permission_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_enterprise(monkeypatch, True)

    async def _denied(**_kwargs):
        return {"allowed": False, "source": "none"}

    monkeypatch.setattr(client_mod, "query_upload_permission", _denied)
    with pytest.raises(client_mod.SkillUploadPermissionError) as exc:
        await guard_mod.assert_skill_upload_permission(
            authorization="Bearer t",
            jiuwenclaw_id="inst-1",
            method="skills.import_upload",
            params={"path": "/tmp/a.zip"},
        )
    assert exc.value.code == client_mod.PERMISSION_REQUIRED


@pytest.mark.asyncio
async def test_manager_failure_propagates_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_enterprise(monkeypatch, True)

    async def _unavailable(**_kwargs):
        raise client_mod.SkillUploadPermissionError(
            client_mod.CHECK_UNAVAILABLE, "permission service unreachable"
        )

    monkeypatch.setattr(client_mod, "query_upload_permission", _unavailable)
    with pytest.raises(client_mod.SkillUploadPermissionError) as exc:
        await guard_mod.assert_skill_upload_permission(
            authorization="Bearer t",
            jiuwenclaw_id="inst-1",
            method="skills.import_upload",
            params={"path": "/tmp/a.zip"},
        )
    assert exc.value.code == client_mod.CHECK_UNAVAILABLE
