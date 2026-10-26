"""Inference privacy proxy API routes.

Provides hot-pluggable proxy management:
- Create/start/stop/update/delete proxies
- Each proxy has a single route (path_prefix -> target_endpoint)
- List proxies and get logs

Validation is handled by Pydantic models in models/policy.py.
Connectivity is not checked at config time - proxy returns 502 if target unreachable at request time.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import ProxyRouteEntry
from jiuwenbox.proxy.inference_privacy_proxy import InferencePrivacyProxyConfig, ProxyRoute
from jiuwenbox.proxy.inference_privacy_proxy_manager import get_proxy_manager

router = APIRouter(tags=["proxies"])
configure_logging()
logger = logging.getLogger(__name__)


def _contains_crlf_or_null(value: str) -> bool:
    """Check if string contains CRLF or null byte."""
    return "\r" in value or "\n" in value or "\x00" in value


def _validate_proxy_name(name: str) -> str:
    """Validate proxy_name URL parameter.
    
    Only allows: alphanumeric, dash (-), underscore (_)
    Prevents log injection and error message injection.
    """
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="proxy_name cannot be empty")
    
    if _contains_crlf_or_null(name):
        raise HTTPException(status_code=400, detail="proxy_name contains invalid characters")
    
    stripped = name.strip()
    for c in stripped:
        if not (c.isalnum() or c in ("-", "_")):
            raise HTTPException(
                status_code=400,
                detail="proxy_name must contain only alphanumeric, dash, or underscore"
            )
    
    return stripped


class RouteRequest(BaseModel):
    path_prefix: str = Field(..., description="Path prefix to match (e.g., /openai)")
    target_endpoint: str = Field(..., description="Target endpoint URL (e.g., https://api.openai.com)")
    api_key: str = Field(default="", description="API key to inject")
    skip_cert_verify: bool = Field(default=False, description="Skip TLS cert verification")


def _mgr():
    return get_proxy_manager()


@router.post("/proxies", status_code=201)
async def create_proxy(route: RouteRequest):
    """Create inference privacy proxy with a single route."""
    
    try:
        validated_entry = ProxyRouteEntry(
            path_prefix=route.path_prefix,
            target_endpoint=route.target_endpoint,
            api_key=route.api_key,
            skip_cert_verify=route.skip_cert_verify,
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    
    proxy_name = validated_entry.path_prefix.lstrip("/").replace("/", "-") or "default"
    
    existing = await _mgr().get_proxy(proxy_name)
    if existing is not None:
        raise HTTPException(status_code=400, detail=f"Route '{validated_entry.path_prefix}' already exists")
    
    proxy_route = ProxyRoute(
        path_prefix=validated_entry.path_prefix,
        target_endpoint=validated_entry.target_endpoint,
        api_key=validated_entry.api_key,
        skip_cert_verify=validated_entry.skip_cert_verify,
    )
    config = InferencePrivacyProxyConfig(routes=[proxy_route])
    
    try:
        result = await _mgr().create_proxy(proxy_name, config)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/proxies")
async def list_proxies():
    """List all inference privacy proxies."""
    return await _mgr().list_proxies()


@router.get("/proxies/{proxy_name}")
async def get_proxy(proxy_name: str):
    """Get a specific proxy's details."""
    validated_name = _validate_proxy_name(proxy_name)
    result = await _mgr().get_proxy(validated_name)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Proxy '{validated_name}' not found")
    return result


@router.delete("/proxies/{proxy_name}")
async def delete_proxy(proxy_name: str):
    """Delete a proxy."""
    validated_name = _validate_proxy_name(proxy_name)
    try:
        result = await _mgr().delete_proxy(validated_name)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/proxies/{proxy_name}/start")
async def start_proxy(proxy_name: str):
    """Start a proxy."""
    validated_name = _validate_proxy_name(proxy_name)
    try:
        result = await _mgr().start_proxy(validated_name)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/proxies/{proxy_name}/stop")
async def stop_proxy(proxy_name: str):
    """Stop a proxy."""
    validated_name = _validate_proxy_name(proxy_name)
    try:
        result = await _mgr().stop_proxy(validated_name)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/proxies/{proxy_name}")
async def update_proxy(proxy_name: str, route: RouteRequest):
    """Update proxy route."""
    validated_name = _validate_proxy_name(proxy_name)
    
    try:
        validated_entry = ProxyRouteEntry(
            path_prefix=route.path_prefix,
            target_endpoint=route.target_endpoint,
            api_key=route.api_key,
            skip_cert_verify=route.skip_cert_verify,
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    
    existing = await _mgr().get_proxy(validated_name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Proxy '{validated_name}' not found")

    proxy_route = ProxyRoute(
        path_prefix=validated_entry.path_prefix,
        target_endpoint=validated_entry.target_endpoint,
        api_key=validated_entry.api_key,
        skip_cert_verify=validated_entry.skip_cert_verify,
    )
    config = InferencePrivacyProxyConfig(
        listen_port=existing["listen_port"],
        listen_host=existing.get("listen_host", "127.0.0.1"),
        routes=[proxy_route],
    )

    try:
        result = await _mgr().update_proxy(validated_name, config)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/proxies/{proxy_name}/logs")
async def get_proxy_logs(
    proxy_name: str,
    lines: int | None = Query(None, description="Number of log lines to return")
):
    """Get logs for a proxy."""
    validated_name = _validate_proxy_name(proxy_name)
    if lines is not None and (lines < 0 or lines > 10000):
        raise HTTPException(status_code=400, detail="lines must be between 0 and 10000")
    try:
        result = await _mgr().get_proxy_logs(validated_name, lines)
        return PlainTextResponse(result["logs"])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
