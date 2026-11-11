#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Local mock of the vLLM-Omni (and MiniMax-H3-Image JSON) generation API.

Speaks the request/response shapes used by
``jiuwenswarm.agents.harness.common.tools.vllm_omni_gen`` and the MiniMax-H3
image client in ``gen_toolkits``:

- GET  {base}/models
- POST {base}/images/generations   (JSON, OpenAI-style)
- POST {base}/images/edits         (JSON MiniMax-H3 or multipart vLLM-Omni)
- POST {base}/videos               (multipart)
- GET  {base}/videos/{id}
- GET  {base}/videos/{id}/content

Returns hard-coded fixtures from ``fixtures/`` next to this file. No network
egress. Default listen address: 127.0.0.1:18091 so
``VISUAL_GEN_API_BASE`` / ``VIDEO_GEN_API_BASE`` can be set to
``http://127.0.0.1:18091/v1``.

Usage:
    python scripts/design_qos_1111/mock_vllm_omni.py [--host 127.0.0.1] [--port 18091]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
IMAGE_PATH = FIXTURES / "image.png"
IMAGE_EDIT_PATH = FIXTURES / "image_edit.png"
VIDEO_PATH = FIXTURES / "video.mp4"

DEFAULT_HOST = os.environ.get("DESIGN_QOS_MOCK_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("DESIGN_QOS_MOCK_PORT", "18091"))
SERVED_IMAGE_MODEL = "Qwen/Qwen-Image-2512"
SERVED_VIDEO_MODEL = "MiniMaxAI/MiniMax-H3"
JOB_ID = "video_mock_fixed"


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


class MockVllmOmniHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[mock-vllm-omni] " + (fmt % args) + "\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, raw, "application/json")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in {"/v1/models", "/models"}:
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": SERVED_IMAGE_MODEL, "object": "model"},
                        {"id": SERVED_VIDEO_MODEL, "object": "model"},
                    ],
                },
            )
            return
        if path in {f"/v1/videos/{JOB_ID}", f"/videos/{JOB_ID}"}:
            self._json(200, {"id": JOB_ID, "status": "completed"})
            return
        if path in {f"/v1/videos/{JOB_ID}/content", f"/videos/{JOB_ID}/content"}:
            data = VIDEO_PATH.read_bytes()
            self._send(200, data, "video/mp4")
            return
        if path in {"/health", "/v1/health"}:
            self._json(200, {"ok": True, "fixtures": str(FIXTURES)})
            return
        self._json(404, {"error": {"message": f"unknown path {path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_body()
        if path in {"/v1/images/generations", "/images/generations"}:
            self._json(
                200,
                {
                    "created": 1,
                    "data": [
                        {
                            "b64_json": _b64(IMAGE_PATH),
                            "size": "64x36",
                            "revised_prompt": _prompt_from(body) or "fixture image",
                        }
                    ],
                    "output_format": "png",
                },
            )
            return
        if path in {"/v1/images/edits", "/images/edits"}:
            self._json(
                200,
                {
                    "created": 1,
                    "data": [
                        {
                            "b64_json": _b64(IMAGE_EDIT_PATH),
                            "size": "64x36",
                            "revised_prompt": _prompt_from(body) or "fixture image edit",
                        }
                    ],
                    "output_format": "png",
                },
            )
            return
        if path in {"/v1/videos", "/videos"}:
            self._json(200, {"id": JOB_ID, "status": "queued"})
            return
        self._json(404, {"error": {"message": f"unknown path {path}", "type": "not_found"}})


def _prompt_from(body: bytes) -> str:
    if not body:
        return ""
    # JSON MiniMax / vLLM-Omni generations
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = body.decode("utf-8", errors="replace")
        marker = 'name="prompt"'
        idx = text.find(marker)
        if idx < 0:
            return ""
        rest = text[idx + len(marker) :]
        # multipart: blank line then value until next boundary
        parts = rest.split("\r\n")
        for i, line in enumerate(parts):
            if line == "" and i + 1 < len(parts):
                return parts[i + 1].strip()
        return ""
    if isinstance(payload, dict):
        return str(payload.get("prompt") or "").strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    for path in (IMAGE_PATH, IMAGE_EDIT_PATH, VIDEO_PATH):
        if not path.is_file():
            print(f"missing fixture: {path}", file=sys.stderr)
            return 1
    server = ThreadingHTTPServer((args.host, args.port), MockVllmOmniHandler)
    url = f"http://{args.host}:{args.port}/v1"
    print(f"mock vLLM-Omni listening on {url}", flush=True)
    print(f"fixtures: {FIXTURES}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
