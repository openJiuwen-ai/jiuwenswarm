import json

import pytest

from jiuwenswarm.server.runtime.marketplace.asset_publish_api import (
    AssetPublishAPI,
    PublishAPIError,
)
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishResult


class Publisher:
    calls = 0

    async def publish(self, request, *, auth):
        self.calls += 1
        return PublishResult(
            "remote-id",
            request.identity,
            "pending_moderation",
            request.visibility,
            False,
        )


@pytest.fixture
def api(tmp_path):
    root = tmp_path / "resources"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\nbody\n"
    )
    publisher = Publisher()

    def resolve(kind, local_id):
        if kind != "skill" or local_id != "demo":
            raise PublishAPIError("RESOURCE_NOT_FOUND")
        return root

    instance = AssetPublishAPI(
        tmp_path / "state",
        publisher=publisher,
        resolver=resolve,
        hub_url="https://example.com",
    )
    yield instance, publisher, tmp_path
    instance.store.close()


def params(**kwargs):
    return {
        "auth": {"access_token": "synthetic-user-token", "oauth_provider": "gitcode"},
        **kwargs,
    }


@pytest.mark.asyncio
async def test_five_calls_and_request_scope_cannot_be_forged(api):
    server, publisher, _ = api
    describe = await server.call(
        "describe", params(kind="skill", local_id="demo"), gateway_user="browser-user"
    )
    assert describe["defaults"]["asset_name"] == "demo"
    assert describe["identity_verified"] is False
    draft = await server.call(
        "prepare",
        params(
            kind="skill",
            local_id="demo",
            metadata={
                "asset_name": "demo",
                "version": "1.0.0",
                "description": "Edited",
                "tags": ["x"],
            },
        ),
        gateway_user="browser-user",
    )
    assert draft["can_submit"] and "path" not in draft
    operation = await server.call(
        "commit",
        params(draft_id=draft["draft_id"], request_id="r1"),
        gateway_user="browser-user",
    )
    await server.service.wait_idle()
    result = await server.call(
        "status",
        params(operation_id=operation["operation_id"]),
        gateway_user="browser-user",
    )
    assert result["result"]["publish_result"] == "pending_moderation"
    history = await server.call(
        "records", params(kind="skill", local_id="demo"), gateway_user="browser-user"
    )
    assert len(history["records"]) == 1 and publisher.calls == 1
    with pytest.raises(PublishAPIError):
        await server.call(
            "status",
            params(operation_id=operation["operation_id"]),
            gateway_user="other-user",
        )
    with pytest.raises(PublishAPIError):
        await server.call(
            "status",
            {
                **params(operation_id=operation["operation_id"]),
                "auth": {"access_token": "other-token", "oauth_provider": "gitcode"},
            },
            gateway_user="browser-user",
        )
    await server.close()


@pytest.mark.asyncio
async def test_prepare_blocks_same_completed_version_until_force_is_enabled(api):
    server, _, _ = api
    metadata = {
        "asset_name": "demo",
        "version": "1.0.0",
        "description": "First release",
        "tags": [],
    }
    first = await server.call(
        "prepare",
        params(kind="skill", local_id="demo", metadata=metadata),
        gateway_user="browser-user",
    )
    await server.call(
        "commit",
        params(draft_id=first["draft_id"], request_id="first-release"),
        gateway_user="browser-user",
    )
    await server.service.wait_idle()

    duplicate = await server.call(
        "prepare",
        params(kind="skill", local_id="demo", metadata=metadata),
        gateway_user="browser-user",
    )
    assert duplicate["can_submit"] is False
    assert duplicate["errors"] == [{"code": "VERSION_CONFLICT", "field": "version"}]

    forced = await server.call(
        "prepare",
        params(kind="skill", local_id="demo", metadata=metadata, force=True),
        gateway_user="browser-user",
    )
    assert forced["can_submit"] is True
    assert forced["errors"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"path": "/etc"},
        {"scope": "forged"},
        {"hub_url": "http://evil"},
        {"publisher_id": "admin"},
    ],
)
async def test_rejects_client_source_scope_and_hub_overrides(api, extra):
    server, _, _ = api
    with pytest.raises(PublishAPIError):
        await server.call(
            "prepare",
            params(
                kind="skill",
                local_id="demo",
                metadata={"asset_name": "demo", "version": "1.0.0"},
                **extra,
            ),
            gateway_user="u",
        )


@pytest.mark.asyncio
async def test_auth_required_safe_errors_and_missing_local_resource(api):
    server, _, _ = api
    with pytest.raises(PublishAPIError, match="AUTH_REQUIRED"):
        await server.call(
            "describe", {"kind": "skill", "local_id": "demo"}, gateway_user="u"
        )
    with pytest.raises(PublishAPIError, match="RESOURCE_NOT_FOUND"):
        await server.call(
            "prepare",
            params(
                kind="skill",
                local_id="../secret",
                metadata={"asset_name": "demo", "version": "1.0.0"},
            ),
            gateway_user="u",
        )


@pytest.mark.asyncio
async def test_scope_is_persistently_bound_without_storing_tokens(api):
    server, _, root = api
    first = server.scope("u", params()["auth"])
    same = AssetPublishAPI(root / "state", hub_url="https://example.com")
    try:
        assert same.scope("u", params()["auth"]) == first
        assert "synthetic-user-token" not in first
        assert server.scope("other", params()["auth"]) != first
    finally:
        same.store.close()


@pytest.mark.parametrize(
    "kind,directory",
    [("plugin", "plugin_packages"), ("agent_template", "agent_templates"),
     ("agent_group", "agent_groups")],
)
def test_real_equipment_resolver_accepts_installed_package(
    tmp_path, monkeypatch, kind, directory
):
    from jiuwenswarm.server.runtime import extension_package_manager as packages
    from jiuwenswarm.server.runtime.marketplace.asset_publish_api import (
        resolve_local_asset,
    )

    monkeypatch.setattr(packages, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(packages, "get_user_workspace_dir", lambda: tmp_path)
    root = (
        tmp_path / ".agent_teams" / directory / "local" / "demo"
        if kind == "agent_group"
        else tmp_path / "plugins" / directory / "local" / "demo"
    )
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": kind,
                "id": "demo",
                "name": "demo",
                "version": "1.0.0",
                "description": "Demo",
            }
        )
    )
    assert resolve_local_asset(kind, "demo") == root


def test_agent_group_publish_resolves_installed_hub_asset_id(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime import extension_package_manager as packages
    from jiuwenswarm.server.runtime.marketplace.asset_publish_api import resolve_local_asset
    from jiuwenswarm.server.runtime.marketplace.hub_install_state import (
        HubInstallRecord,
        HubInstallStateStore,
    )

    monkeypatch.setattr(packages, "get_user_workspace_dir", lambda: tmp_path)
    root = tmp_path / ".agent_teams" / "agent_groups" / "local" / "business-planning"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps({"package_type": "agent_group", "name": "business-planning"})
    )
    HubInstallStateStore(root.parent.parent).upsert(
        HubInstallRecord(
            asset_id="b80afb7afff147bd801ed5f788fa767c",
            kind="agent_group",
            package_id="business-planning",
            version="1.2.0",
            checksum_sha256="checksum",
            installed_at="2026-09-18T00:00:00Z",
        )
    )

    assert resolve_local_asset("agent_group", "b80afb7afff147bd801ed5f788fa767c") == root


@pytest.mark.asyncio
async def test_agent_group_describe_uses_local_group_manifest(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({
        "package_type": "agent_group",
        "name": "demo",
        "version": "0.2.0",
        "display_name": "Demo Team",
        "description": "A team of experts",
        "agents": ["leader"],
    }))
    instance = AssetPublishAPI(
        tmp_path / "state",
        publisher=Publisher(),
        resolver=lambda kind, local_id: root,
        hub_url="https://example.com",
    )
    try:
        result = await instance.call(
            "describe",
            params(kind="agent_group", local_id="demo"),
            gateway_user="browser-user",
        )
        assert result["defaults"] == {
            "asset_name": "demo",
            "version": "0.2.0",
            "display_name": "Demo Team",
            "description": "A team of experts",
            "tags": [],
            "visibility": "public",
        }
    finally:
        instance.store.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com",
        "https://example.com?token=secret",
        "https://example.com#secret",
    ],
)
def test_destination_must_not_expose_credentials(tmp_path, url):
    with pytest.raises(PublishAPIError, match="INVALID_HUB_CONFIGURATION"):
        AssetPublishAPI(tmp_path, publisher=Publisher(), hub_url=url)


@pytest.mark.asyncio
async def test_local_status_needs_no_hub_login_and_does_not_expose_records(api):
    server, publisher, _ = api
    ref = {"kind": "skill", "local_id": "demo"}
    assert await server.call("local_status", ref, gateway_user="browser-user") == {
        "state": "unknown"
    }
    draft = await server.call(
        "prepare",
        params(
            **ref,
            metadata={"asset_name": "demo", "version": "1.0.0", "description": "Demo"},
        ),
        gateway_user="browser-user",
    )
    await server.call(
        "commit",
        params(draft_id=draft["draft_id"], request_id="local-status-test"),
        gateway_user="browser-user",
    )
    await server.service.wait_idle()
    assert await server.call("local_status", ref, gateway_user="browser-user") == {
        "state": "pending"
    }
    assert publisher.calls == 1
    # Only a minimal workspace asset status is available without Hub authentication.
    with pytest.raises(PublishAPIError):
        await server.call("records", ref, gateway_user="browser-user")
    with pytest.raises(PublishAPIError):
        await server.call(
            "commit",
            {"draft_id": draft["draft_id"], "request_id": "no-auth"},
            gateway_user="browser-user",
        )


@pytest.mark.asyncio
async def test_local_status_checks_asset_reference(api):
    server, _, _ = api
    with pytest.raises(PublishAPIError):
        await server.call(
            "local_status",
            {"kind": "skill", "local_id": "missing"},
            gateway_user="browser-user",
        )
