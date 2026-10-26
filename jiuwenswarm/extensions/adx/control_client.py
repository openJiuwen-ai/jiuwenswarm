# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Public Binding management and generic Execd forwarding through ADX Ingress."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from jiuwenswarm.extensions.adx.config import AdxRouterConfig


class AdxControlError(RuntimeError):
    def __init__(self, message: str, *, code: str = "SERVICE_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


class AdxControlClient:
    def __init__(self, config: AdxRouterConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.http = httpx.AsyncClient(
            timeout=config.connect_timeout_seconds, follow_redirects=False, transport=transport,
        )

    def scope(self, user: str) -> dict[str, str]:
        cfg = self.config
        return {"tenant": cfg.tenant, "template": cfg.template, "version": cfg.version,
                "binding_id": cfg.binding_id(user)}

    def parameters(self, user: str) -> dict[str, str]:
        digest = hashlib.sha256(json.dumps(
            [self.config.tenant, self.config.template, user], ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        return {**self.config.parameters, "workspace": "w-" + digest}

    async def close(self) -> None:
        await self.http.aclose()

    def _binding_path(self, user: str) -> str:
        cfg = self.config
        return "/api/agent/v2/templates/{}/versions/{}/bindings/{}".format(
            quote(cfg.template, safe=""), quote(cfg.version, safe=""), quote(cfg.binding_id(user), safe=""),
        )

    async def _binding_request(self, user: str, method: str) -> httpx.Response:
        try:
            response = await self.http.request(
                method, urljoin(self.config.ingress_url, self._binding_path(user)),
                headers={"Authorization": "Bearer " + self.config.token},
            )
        except httpx.RequestError as exc:
            raise AdxControlError("ADX management outcome is unknown; reuse the same Binding") from exc
        if response.status_code >= 300 and not (method == "DELETE" and response.status_code == 404):
            raise AdxControlError(f"ADX Binding {method} returned HTTP {response.status_code}")
        return response

    async def binding_environment(self, user: str) -> str:
        response = await self._binding_request(user, "GET")
        try:
            binding = response.json()["binding"]
            environment = binding["environment_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AdxControlError("Invalid ADX Binding response") from exc
        valid_environment = isinstance(environment, str) and environment.startswith("adx-")
        if (binding.get("scope") != self.scope(user) or binding.get("phase") != "active"
                or not valid_environment):
            raise AdxControlError("ADX Binding identity or state mismatch", code="CONFLICT")
        return environment

    async def delete_binding(self, user: str) -> None:
        await self._binding_request(user, "DELETE")

    async def invoke(self, environment: str, action: str, args: dict) -> dict:
        rid = "invoke-" + uuid.uuid4().hex
        try:
            response = await self.http.post(
                urljoin(self.config.ingress_url, f"/direct/{environment}/invoke"),
                headers={"Authorization": "Bearer " + self.config.token, "X-ADX-Request-ID": rid},
                json={"action": action, "args": args, "requestId": rid},
            )
        except httpx.RequestError as exc:
            raise AdxControlError("ADX operation outcome is unknown; operation was not replayed") from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise AdxControlError("Invalid ADX operation response") from exc
        if not isinstance(result, dict):
            raise AdxControlError("Invalid ADX operation response")
        if result.get("error") and response.status_code in (200, 400, 404):
            message = str(result["error"])
            missing = "not found" in message.lower() or "no such file" in message.lower()
            code = "NOT_FOUND" if missing else "INTERNAL_ERROR"
            raise AdxControlError(message, code=code)
        if response.status_code >= 300:
            raise AdxControlError(f"ADX operation returned HTTP {response.status_code}")
        return result
