from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import uuid

from jiuwenclaw.e2a.adapters import (
    e2a_response_to_acp_jsonrpc_response,
    envelope_from_acp_jsonrpc,
)
from jiuwenclaw.e2a.constants import E2A_RESPONSE_KIND_E2A_CHUNK
from jiuwenclaw.e2a.models import E2AResponse

logger = logging.getLogger(__name__)


def write_json_stdout(payload: dict) -> None:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiuwenclaw-cli",
        description="JiuwenClaw CLI 入口（子命令分发）。",
    )
    subparsers = parser.add_subparsers(dest="command")

    acp_parser = subparsers.add_parser("acp", help="ACP stdio 入口。")
    acp_parser.add_argument(
        "--gateway-url",
        default=None,
        help="AgentServer WebSocket URL，传递给 gateway stdio 子进程。",
    )
    acp_parser.add_argument(
        "--session-id",
        default="acp_cli_session",
        help="ACP 请求使用的 session_id。",
    )
    acp_parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Prompt 内容。",
    )
    return parser


def run_acp(args: argparse.Namespace) -> int:
    prompt = " ".join(list(args.args or [])).strip()
    if not prompt:
        print("acp prompt is required", file=sys.stderr)
        return 2

    request_id = f"acp_cli_{uuid.uuid4().hex[:12]}"
    env = envelope_from_acp_jsonrpc(
        method="session/prompt",
        params={"content": prompt},
        jsonrpc_id=request_id,
        session_id=str(args.session_id or "acp_cli_session"),
        channel="acp",
    )
    env.request_id = request_id
    env.is_stream = True

    cmd = [sys.executable, "-m", "jiuwenclaw.channel.acp_channel"]
    gateway_url = getattr(args, "gateway_url", None) or getattr(args, "agent_server_url", None)
    if gateway_url:
        cmd.extend(["--gateway-url", str(gateway_url)])

    logger.info("[CLI] starting ACP stdio gateway: %s", cmd)
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        encoding="utf-8",
        env=child_env,
    )

    final_rpc: dict | None = None
    try:
        #assert proc.stdin is not None
        proc.stdin.write(json.dumps(env.to_dict(), ensure_ascii=False) + "\n")
        proc.stdin.flush()
        proc.stdin.close()

        #assert proc.stdout is not None
        for line in proc.stdout:
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("[CLI] skip non-json stdout: %s", raw)
                continue

            resp = E2AResponse.from_dict(data)
            if resp.response_kind == E2A_RESPONSE_KIND_E2A_CHUNK:
                continue
            if resp.is_final:
                final_rpc = e2a_response_to_acp_jsonrpc_response(resp)
                if final_rpc is None:
                    final_rpc = {
                        "jsonrpc": "2.0",
                        "id": resp.jsonrpc_id,
                        "result": resp.body,
                    }
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    if final_rpc is None:
        write_json_stdout(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "no final response from gateway"},
            }
        )
        return 1

    write_json_stdout(final_rpc)
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "acp":
        raise SystemExit(run_acp(args))

    parser.print_help(sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
