"""用真实 MCP SDK + 真实 HTTP 403 验证「一次说清、不要空转」的行为。

与 ``verify_mcp_init_failure_signal.py`` 的区别：那个脚本把 session 工厂换成 mock，
只验证 worker 边界；这里起一个本地假 MCP 端点，对 ``initialize`` 直接返回 403，
走真实 ``StreamableHttpClient`` 建连，因此同时验证：

1. 真实 SDK 抛出的 403 异常能被识别为鉴权失败（而不是「一连串无信息的 interrupted」）；
2. 调用者拿到的文案带可执行指引；
3. 之后同一请求再 acquire 立即失败（耗时远小于 shutdown 的 5s grace），不再空转。

用法（仓库根目录）：

    .venv/Scripts/python.exe scripts/verify_mcp_auth_failure_live.py

只监听 127.0.0.1 的临时端口，不读写任何配置，不启动 Agent Server。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REQUEST_ID = "verify-live-req"
SERVER_NAME = "xlsx-mcp"


class _ForbiddenHandler(BaseHTTPRequestHandler):
    """所有 JSON-RPC 请求一律 403，模拟凭证过期后的 xlsx-mcp。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib 回调命名
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "error": {"code": -32001, "message": "XLSX_CAPABILITY_INVALID"},
            }
        ).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:  # 静音 stdlib 访问日志
        return


def _start_server() -> tuple[ThreadingHTTPServer, int, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForbiddenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], thread


async def run(root: Path) -> int:
    sys.path.insert(0, str(root))
    from jiuwenswarm.common import mcp_call_timeout_patch, mcp_config

    # 运行时由 JiuWenSwarmDeepAdapter.__init__ 安装；脚本里显式装一次，保证
    # HTTP 状态观察钩子（也就是 403 诊断）和线上走同一条路径。
    mcp_call_timeout_patch.apply_mcp_call_timeout_patch()

    server, port, _thread = _start_server()
    url = f"http://127.0.0.1:{port}/mcp"
    params = {
        "_mcp_client_type": "streamable-http",
        "server_name": SERVER_NAME,
        "server_id": SERVER_NAME,
        "server_path": url,
        "params": {"timeout_s": 5},
    }
    failures = 0
    try:
        print(f"== fake MCP endpoint: {url} (every request -> HTTP 403)")
        worker = await mcp_config.acquire_request_scoped_mcp_session(
            REQUEST_ID, SERVER_NAME, params
        )
        request = mcp_config._McpCallRequest("get_workbook_context", {})
        worker.queue.put_nowait(request)
        result = (await asyncio.gather(request.future, return_exceptions=True))[0]
        print(f"   caller got: {type(result).__name__}: {result}")

        message = str(result)
        if "HTTP 403" not in message:
            print("   FAIL: the real SDK 403 was not recognised as an auth failure")
            failures += 1
        if "Do not retry" not in message or "resend" not in message:
            print("   FAIL: no actionable guidance for the model")
            failures += 1
        if (REQUEST_ID, SERVER_NAME) not in mcp_config._request_scoped_mcp_dead:
            print("   FAIL: connector not marked unrecoverable")
            failures += 1

        print("== same request, second acquire must fail fast")
        started = time.monotonic()
        try:
            await mcp_config.acquire_request_scoped_mcp_session(
                REQUEST_ID, SERVER_NAME, params
            )
            print("   FAIL: acquire did not raise")
            failures += 1
        except mcp_config.McpWorkerStoppedError as exc:
            elapsed = time.monotonic() - started
            print(f"   caller got: McpWorkerStoppedError in {elapsed * 1000:.1f} ms")
            if elapsed > 1.0:
                print("   FAIL: fast-fail took longer than 1s (looks like a rebuild)")
                failures += 1
    finally:
        await mcp_config.release_request_scoped_mcp_sessions(REQUEST_ID)
        server.shutdown()
        server.server_close()

    print(
        f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}"
    )
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    return asyncio.run(run(Path(args.root).resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
