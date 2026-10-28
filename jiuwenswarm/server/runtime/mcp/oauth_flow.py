# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-managed OAuth Authorization Code + PKCE flows for remote MCP packages."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import httpx

from jiuwenswarm.common.mcp_oauth import McpOAuthError
from jiuwenswarm.common.utils import get_workspace_dir
from jiuwenswarm.server.runtime.mcp.credential import CredentialStore


_FLOW_TTL_S = 10 * 60
_HTTP_TIMEOUT_S = 20.0
_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"

logger = logging.getLogger(__name__)

# Canva is currently the only package using this flow. Keeping the provider
# metadata in code avoids shipping a client registration or token endpoint in
# a package that can be edited locally.
_PROVIDERS: dict[str, dict[str, Any]] = {
    "canva": {
        "mcp_url": "https://mcp.canva.cn/mcp",
        "client_name": "JiuwenSwarm Canva Connector",
        "scope": " ".join(
            [
                "profile:read",
                "design:meta:read",
                "design:content:write",
                "design:content:read",
                "folder:read",
                "folder:write",
                "brandtemplate:content:read",
                "brandtemplate:meta:read",
                "brandtemplate:content:write",
                "comment:write",
                "comment:read",
                "asset:read",
                "asset:write",
                "brandkit:read",
                "help:answers:read",
                "help:answers:write",
            ]
        ),
    }
}

_TOKEN_KEYS = ("access_token", "refresh_token", "expires_in", "token_type", "scope")


@dataclass
class OAuthFlow:
    name: str
    provider: dict[str, Any]
    metadata: dict[str, Any]
    client_id: str
    redirect_uri: str
    state: str
    code_verifier: str
    created_at: float
    status: str = "pending"
    result: dict[str, Any] | None = None


@dataclass
class OAuthCredentials:
    client_id: str
    access_token: str
    refresh_token: str = ""


def _current_time() -> float:
    return time.time()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _get_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise McpOAuthError(f"OAuth server returned invalid JSON ({response.status_code})") from exc
    if not isinstance(payload, dict):
        raise McpOAuthError("OAuth server response must be a JSON object")
    return payload


def _authorization_servers(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get("authorization_servers")
    if isinstance(raw, list):
        return [str(item).rstrip("/") for item in raw if str(item).strip()]
    return []


def _authorization_server_url(mcp_url: str, metadata: dict[str, Any]) -> str:
    servers = _authorization_servers(metadata)
    if not servers:
        parsed = urlsplit(mcp_url)
        if not parsed.scheme.startswith("http") or not parsed.netloc:
            raise McpOAuthError("MCP URL is invalid")
        return f"{parsed.scheme}://{parsed.netloc}"
    if len(servers) != 1:
        raise McpOAuthError("Only one MCP authorization server is supported")
    return servers[0]


def _validate_metadata(metadata: dict[str, Any]) -> None:
    required = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
    missing = [key for key in required if not str(metadata.get(key) or "").strip()]
    if missing:
        raise McpOAuthError(f"OAuth metadata missing: {', '.join(missing)}")
    if "code" not in [str(item) for item in metadata.get("response_types_supported", [])]:
        raise McpOAuthError("OAuth server does not support the authorization code flow")
    if "none" not in [str(item) for item in metadata.get("token_endpoint_auth_methods_supported", [])]:
        raise McpOAuthError("OAuth server does not support public clients")


def _register_client(
    client: httpx.Client,
    registration_endpoint: str,
    provider: dict[str, Any],
    redirect_uri: str,
) -> str:
    payload = {
        "client_name": provider["client_name"],
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": provider["scope"],
    }
    response = client.post(registration_endpoint, json=payload)
    if response.status_code not in range(200, 300):
        raise McpOAuthError(f"OAuth client registration failed ({response.status_code})")
    body = _get_json(response)
    client_id = str(body.get("client_id") or "").strip()
    if not client_id:
        raise McpOAuthError("OAuth client registration returned no client_id")
    return client_id


def _parse_callback_url(callback_url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(callback_url)
        port = parsed.port
    except ValueError as exc:
        raise McpOAuthError("OAuth callback must be a loopback base URL") from exc
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port:
        raise McpOAuthError("OAuth callback must be a loopback base URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise McpOAuthError("OAuth callback must be a loopback base URL")
    return parsed.hostname, port


def load_oauth_credentials(name: str) -> OAuthCredentials | None:
    """Return locally stored OAuth credentials for an MCP package."""
    n = str(name or "").strip()
    if not n:
        return None
    stored = CredentialStore(workspace_dir=get_workspace_dir()).get_all(n)
    access_token = stored.get("OAUTH_ACCESS_TOKEN", "")
    client_id = stored.get("OAUTH_CLIENT_ID", "")
    if not access_token or not client_id:
        return None
    return OAuthCredentials(
        client_id=client_id,
        access_token=access_token,
        refresh_token=stored.get("OAUTH_REFRESH_TOKEN", ""),
    )


def clear_oauth_credentials(name: str) -> None:
    CredentialStore(workspace_dir=get_workspace_dir()).delete_mcp(str(name or "").strip())


class OAuthFlowManager:
    """In-memory pending flows; completed tokens are persisted encrypted."""

    def __init__(
        self,
        workspace_dir=None,
        *,
        time_fn: Callable[[], float] | None = None,
        http_client: Callable[[], httpx.Client] | None = None,
    ):
        self._workspace_dir = workspace_dir if workspace_dir is not None else get_workspace_dir()
        self._time_fn = time_fn or _current_time
        self._lock = threading.Lock()
        self._flows: dict[str, OAuthFlow] = {}
        self._client_factory = http_client
        self._flow_path = self._workspace_dir / "mcp" / "oauth_flows.json"
        self._load_flows()

    def _load_flows(self) -> None:
        try:
            raw = json.loads(self._flow_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.debug("[MCP OAuth] No persisted flows to restore")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning("[MCP OAuth] Cannot restore persisted flows (%s)", type(exc).__name__)
        else:
            flows = raw.get("flows") if isinstance(raw, dict) else None
            if not isinstance(flows, dict):
                logger.warning("[MCP OAuth] Persisted flows have an invalid structure")
                return
            for flow_id, payload in flows.items():
                if not isinstance(payload, dict):
                    logger.warning("[MCP OAuth] Skipping a persisted flow with an invalid structure")
                    continue
                try:
                    flow = OAuthFlow(
                        name=str(payload["name"]),
                        provider=dict(payload["provider"]),
                        metadata=dict(payload["metadata"]),
                        client_id=str(payload["client_id"]),
                        redirect_uri=str(payload["redirect_uri"]),
                        state=str(payload["state"]),
                        code_verifier=str(payload["code_verifier"]),
                        created_at=float(payload["created_at"]),
                        status=str(payload.get("status") or "pending"),
                        result=payload.get("result"),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("[MCP OAuth] Skipping an invalid persisted flow (%s)", type(exc).__name__)
                else:
                    self._flows[str(flow_id)] = flow

    def _save_flows(self) -> None:
        payload = {
            "version": 1,
            "flows": {
                flow_id: {
                    "name": flow.name,
                    "provider": flow.provider,
                    "metadata": flow.metadata,
                    "client_id": flow.client_id,
                    "redirect_uri": flow.redirect_uri,
                    "state": flow.state,
                    "code_verifier": flow.code_verifier,
                    "created_at": flow.created_at,
                    "status": flow.status,
                    "result": flow.result,
                }
                for flow_id, flow in self._flows.items()
            },
        }
        self._flow_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._flow_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._flow_path)

    def _store(self) -> CredentialStore:
        return CredentialStore(workspace_dir=self._workspace_dir)

    def _client(self) -> httpx.Client:
        if self._client_factory is not None:
            return self._client_factory()
        return httpx.Client(
            timeout=_HTTP_TIMEOUT_S,
            headers={"Accept": "application/json", "User-Agent": "JiuwenSwarm/1.0"},
            trust_env=True,
        )

    def _prune_locked(self) -> None:
        now = self._time_fn()
        for key, flow in list(self._flows.items()):
            if now - flow.created_at > _FLOW_TTL_S or flow.status in {"complete", "failed"}:
                del self._flows[key]

    @staticmethod
    def _provider(name: str) -> dict[str, Any]:
        provider = _PROVIDERS.get(name)
        if provider is None:
            raise McpOAuthError(f"MCP '{name}' does not support host OAuth")
        return provider

    def start(self, name: str, callback_url: str) -> dict[str, Any]:
        """Discover metadata, dynamically register a client, and build the URL."""
        n = str(name or "").strip()
        if not n:
            raise McpOAuthError("MCP name is required")
        _host, port = _parse_callback_url(callback_url)

        provider = self._provider(n)
        flow_id = secrets.token_urlsafe(24)
        state = secrets.token_urlsafe(32)
        verifier, challenge = _make_pkce()
        redirect_uri = f"http://127.0.0.1:{port}/mcp/oauth/callback"
        if n == "canva":
            redirect_uri = "http://127.0.0.1:19000/mcp/oauth/callback"

        with self._client() as client:
            resource_url = _authorization_server_url(provider["mcp_url"], {})
            resource_response = client.get(f"{resource_url}{_PROTECTED_RESOURCE_PATH}")
            if resource_response.status_code != 200:
                raise McpOAuthError(f"OAuth discovery failed ({resource_response.status_code})")
            resource_metadata = _get_json(resource_response)
            server_url = _authorization_server_url(provider["mcp_url"], resource_metadata)
            metadata_response = client.get(f"{server_url}/.well-known/oauth-authorization-server")
            if metadata_response.status_code != 200:
                raise McpOAuthError(f"OAuth metadata fetch failed ({metadata_response.status_code})")
            metadata = _get_json(metadata_response)
            _validate_metadata(metadata)
            client_id = _register_client(
                client, str(metadata["registration_endpoint"]), provider, redirect_uri
            )

        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": provider["scope"],
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": provider["mcp_url"],
            }
        )
        authorize_url = f"{metadata['authorization_endpoint']}?{query}"
        flow = OAuthFlow(
            name=n,
            provider=provider,
            metadata=metadata,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_verifier=verifier,
            created_at=self._time_fn(),
        )
        with self._lock:
            self._prune_locked()
            self._flows[flow_id] = flow
            self._save_flows()
        return {
            "auth_required": True,
            "auth_url": authorize_url,
            "flow": flow_id,
            "redirect_uri": redirect_uri,
        }

    def _pop_flow(self, flow_id: str) -> OAuthFlow:
        with self._lock:
            self._prune_locked()
            flow = self._flows.get(flow_id)
            if flow is None:
                self._load_flows()
                flow = self._flows.get(flow_id)
            if flow is None:
                raise McpOAuthError("OAuth flow is unknown or expired")
            if self._time_fn() - flow.created_at > _FLOW_TTL_S:
                self._flows.pop(flow_id, None)
                self._save_flows()
                raise McpOAuthError("OAuth flow expired; retry the connection")
            if flow.status != "pending":
                raise McpOAuthError("OAuth flow is already completed")
            flow.status = "processing"
            self._save_flows()
            return flow

    def _find_flow_by_state(self, state: str) -> tuple[str, OAuthFlow] | None:
        with self._lock:
            self._prune_locked()
            for flow_id, flow in self._flows.items():
                if hmac.compare_digest(flow.state, state):
                    return flow_id, flow
            self._load_flows()
            self._prune_locked()
            for flow_id, flow in self._flows.items():
                if hmac.compare_digest(flow.state, state):
                    if self._time_fn() - flow.created_at > _FLOW_TTL_S:
                        self._flows.pop(flow_id, None)
                        self._save_flows()
                        return None
                    return flow_id, flow
            return None

    def _fail_flow(self, flow_id: str) -> None:
        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is not None:
                flow.status = "failed"
            self._flows.pop(flow_id, None)
            self._save_flows()

    def complete(self, query: dict[str, Any]) -> bool:
        """Validate the callback and exchange the authorization code for tokens."""
        flow_id = str(query.get("flow") or "")
        state = str(query.get("state") or "")
        if flow_id:
            flow = self._pop_flow(flow_id)
        else:
            found = self._find_flow_by_state(state)
            if found is None:
                raise McpOAuthError("OAuth flow is unknown or expired")
            flow_id, flow = found
            with self._lock:
                if flow.status != "pending":
                    raise McpOAuthError("OAuth flow is already completed")
                flow.status = "processing"
                self._save_flows()
        try:
            if not hmac.compare_digest(flow.state, state):
                raise McpOAuthError("OAuth state mismatch; retry the connection")
            error = str(query.get("error") or "")
            if error:
                raise McpOAuthError(f"MCP '{flow.name}' authorization failed: {error}")
            code = str(query.get("code") or "")
            if not code:
                raise McpOAuthError("OAuth callback is missing the authorization code")
        except Exception:
            self._fail_flow(flow_id)
            raise

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": flow.redirect_uri,
            "client_id": flow.client_id,
            "code_verifier": flow.code_verifier,
        }
        try:
            with self._client() as client:
                response = client.post(str(flow.metadata["token_endpoint"]), data=form)
                if response.status_code != 200:
                    raise McpOAuthError(f"OAuth token exchange failed ({response.status_code})")
                body = _get_json(response)
        except Exception:
            self._fail_flow(flow_id)
            raise
        access_token = str(body.get("access_token") or "")
        if not access_token:
            self._fail_flow(flow_id)
            raise McpOAuthError("OAuth token response is missing access_token")

        store = self._store()
        store.save_token(flow.name, "OAUTH_CLIENT_ID", flow.client_id)
        store.save_token(flow.name, "OAUTH_ACCESS_TOKEN", access_token)
        refresh_token = str(body.get("refresh_token") or "")
        if refresh_token:
            store.save_token(flow.name, "OAUTH_REFRESH_TOKEN", refresh_token)
        for key in ("expires_in", "token_type", "scope"):
            if body.get(key) is not None:
                store.save_token(flow.name, f"OAUTH_{key.upper()}", str(body[key]))
        with self._lock:
            flow.status = "complete"
            self._flows.pop(flow_id, None)
            self._save_flows()
        return True


_manager = OAuthFlowManager()


def start_oauth_flow(name: str, callback_url: str) -> dict[str, Any]:
    return _manager.start(name, callback_url)


def complete_oauth_flow(query: dict[str, Any]) -> bool:
    return _manager.complete(query)


def is_host_oauth_mcp(name: str) -> bool:
    from jiuwenswarm.server.runtime.mcp.registry import _resolve_package

    package = _resolve_package(name)
    return package is not None and package.credentials_type == "oauth"


__all__ = [
    "McpOAuthError",
    "OAuthCredentials",
    "OAuthFlowManager",
    "clear_oauth_credentials",
    "complete_oauth_flow",
    "is_host_oauth_mcp",
    "load_oauth_credentials",
    "start_oauth_flow",
]
