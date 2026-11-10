# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit presentation control through the running Gateway's local channel."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from jiuwenswarm.channels.cli.extension import ClientError, JsonArgumentParser, LocalClient, Settings, valid_id


def parser() -> argparse.ArgumentParser:
    command = JsonArgumentParser(description="查询或启动当前实例的形态 / Query or start a presentation in this instance")
    command.add_argument("action", choices=("query", "start"))
    command.add_argument("package_id", nargs="?")
    command.add_argument("instance_key", nargs="?")
    command.add_argument("--connection-id")
    command.add_argument("--revision", type=int)
    command.add_argument("--json", action="store_true")
    command.add_argument("--timeout", type=float, default=40.0)
    return command


async def execute(args: argparse.Namespace) -> tuple[dict, int]:
    if args.package_id is not None:
        valid_id(args.package_id)
    if args.action == "query":
        if args.instance_key is not None or args.connection_id is not None or args.revision is not None:
            raise ClientError("INVALID_INPUT", "query 只接受 package_id / query only accepts package_id")
        params = {"package_id": args.package_id} if args.package_id is not None else {}
    else:
        if not args.package_id or args.instance_key is None or not args.connection_id or args.revision is None:
            raise ClientError("INVALID_INPUT", "start 需要查询得到的完整目标 / start requires the complete queried target")
        if args.timeout <= 30:
            raise ClientError("INVALID_INPUT", "timeout 必须大于 30 秒 / timeout must exceed 30 seconds")
        params = {"package_id": args.package_id, "instance_key": args.instance_key,
                  "connection_id": args.connection_id, "revision": args.revision}
    settings = Settings.from_args(args, domain="gateway")
    client = LocalClient(settings)
    try:
        payload = await client.request(f"presentation.{args.action}", params)
    except ClientError as error:
        unknown = error.uncertain or error.payload.get("status") == "unknown"
        return {"ok": False, "status": "UNKNOWN" if unknown else "FAILED", "error": error.as_dict(),
                "result": error.payload}, 3 if unknown else 1
    return {"ok": True, "status": "SUCCESS", "result": payload}, 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result, code = asyncio.run(execute(parser().parse_args(argv)))
    except ClientError as error:
        result, code = {"ok": False, "status": "FAILED", "error": error.as_dict()}, 2
    except KeyboardInterrupt:
        result, code = {"ok": False, "status": "UNKNOWN", "error": {"code": "INTERRUPTED"}}, 3
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
