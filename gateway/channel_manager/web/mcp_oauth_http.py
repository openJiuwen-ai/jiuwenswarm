# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loopback HTTP callback for host-managed MCP OAuth flows."""

from __future__ import annotations

import html
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from gateway.common.mcp_oauth import McpOAuthError, complete_oauth_flow_async

logger = logging.getLogger(__name__)


def _landing(ok: bool, title: str, detail: str, status: int = 200) -> HTMLResponse:
    close_script = "setTimeout(function(){window.close();},1200);" if ok else ""
    body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{margin:0;font:15px/1.7 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f5f7f9;color:#141a21;display:flex;min-height:100vh;align-items:center;justify-content:center}}
main{{max-width:28rem;padding:2.5rem 2rem;text-align:center}}h1{{font-size:1.25rem;margin:0 0 .5rem}}
p{{margin:0;color:#58646f}}@media(prefers-color-scheme:dark){{body{{background:#101419;color:#e4e9ef}}
p{{color:#a0abb7}}}}</style></head><body><main><h1>{html.escape(title)}</h1>
<p>{html.escape(detail)}</p></main><script>{close_script}</script></body></html>"""
    return HTMLResponse(
        body,
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def attach_mcp_oauth_routes(app: FastAPI) -> None:
    """Register ``/mcp/oauth/callback`` on the WebChannel port."""

    @app.get("/mcp/oauth/callback", name="mcp-oauth-callback", include_in_schema=False)
    async def mcp_oauth_callback(request: Request) -> HTMLResponse:
        query = dict(request.query_params)
        try:
            # 统一异步入口：本地桥（to_thread 语义）与 WS 远端 provider 同签名
            await complete_oauth_flow_async(query)
        except McpOAuthError as exc:
            logger.warning("[MCP OAuth] callback failed: %s", exc)
            return _landing(False, "授权未完成", str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            logger.error("[MCP OAuth] callback crashed (%s)", type(exc).__name__)
            return _landing(False, "授权未完成", "回调处理失败，请检查服务状态后重新连接。", 500)
        return _landing(True, "授权成功", "请回到 JiuwenSwarm 继续使用；本页面会自动关闭。")
