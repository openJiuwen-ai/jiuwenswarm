# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read-only Hub checks used by the member-first publishing coordinator."""

from __future__ import annotations

from urllib.parse import quote
import re

from .hub_client import HubClient, HubNotFoundError, HubProtocolError, HttpHubTransport
from .hub_publish_client import PublishAuth


class _PublisherTransport(HttpHubTransport):
    def __init__(self, base_url: str, auth: PublishAuth):
        super().__init__(base_url=base_url, token=auth.access_token)
        self._provider = auth.oauth_provider

    def _headers(self) -> dict[str, str]:
        headers = super()._headers() or {}
        if self._provider:
            headers["X-OAuth-Provider"] = self._provider
        return headers


class SkillPackPublishHub:
    """Keep authenticated moderation reads separate from anonymous availability."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.public = HubClient(base_url=base_url)

    async def publisher_id(self, auth: PublishAuth) -> str:
        data = await _PublisherTransport(self.base_url, auth).get_data("/api/v1/auth/me")
        value = data.get("id")
        if not isinstance(value, (str, int)) or not str(value).strip():
            raise HubProtocolError("Missing publisher identity")
        return str(value)

    async def find_owned(self, name: str, publisher_id: str, auth: PublishAuth) -> list[dict]:
        """Compare exact names; do not treat semantic search as a namespace check."""
        transport = _PublisherTransport(self.base_url, auth)
        matches = []
        for page in range(1, 101):
            data = await transport.get_data(
                "/api/v1/plugins",
                params={"publisher_id": publisher_id, "page": page, "page_size": 100},
            )
            items = data.get("items")
            if not isinstance(items, list):
                raise HubProtocolError("Invalid publisher catalog")
            for item in items:
                if not isinstance(item, dict) or str(item.get("publisher_id")) != publisher_id:
                    raise HubProtocolError("Publisher catalog identity mismatch")
                if item.get("name") == name:
                    matches.append(item)
            total = data.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise HubProtocolError("Invalid publisher catalog size")
            if page * 100 >= total:
                return matches
        raise HubProtocolError("Publisher catalog exceeds safe enumeration limit")

    async def review(self, asset_id: str, version: str, auth: PublishAuth) -> dict:
        """Return the exact upload's review state, including rejected/pending versions."""
        data = await _PublisherTransport(self.base_url, auth).get_data(
            f"/api/v1/plugins/{quote(asset_id, safe='')}/versions/{quote(version, safe='')}"
        )
        if data.get("asset_id") != asset_id or data.get("version") != version:
            raise HubProtocolError("Review identity mismatch")
        return data

    async def public_artifact(self, asset_id: str, version: str, expected_type: str) -> dict:
        """Anonymous exact-version reads prove public approval and download access."""
        detail = await self.public.get_version(asset_id, version)
        artifact = await self.public.get_artifact(asset_id, version=version, is_cli_download=True)
        if detail.plugin_type != expected_type or artifact.plugin_type != expected_type:
            raise HubProtocolError("Unexpected member or Pack type")
        if not re.fullmatch(r"[0-9a-f]{64}", artifact.checksum_sha256):
            raise HubProtocolError("Original artifact checksum is required")
        return {
            "asset_id": asset_id,
            "name": detail.name,
            "version": version,
            "artifact_sha256": artifact.checksum_sha256,
        }

    async def recover_upload(
        self, name: str, version: str, checksum: str, publisher_id: str, auth: PublishAuth,
    ) -> dict | None:
        """An uncertain POST is never replayed without an exact remote match."""
        matches = await self.find_owned(name, publisher_id, auth)
        if len(matches) != 1:
            return None
        asset_id = matches[0].get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise HubProtocolError("Missing asset identity")
        try:
            detail = await self.review(asset_id, version, auth)
        except HubNotFoundError:
            return None
        # The owner may download its pending/rejected original upload. Never use
        # those credentials for the public-availability check above.
        client = HubClient(transport=_PublisherTransport(self.base_url, auth), base_url=self.base_url)
        artifact = await client.get_artifact(asset_id, version=version, is_cli_download=True)
        if artifact.checksum_sha256 != checksum:
            raise HubProtocolError("Existing upload does not match the confirmed snapshot")
        return detail
