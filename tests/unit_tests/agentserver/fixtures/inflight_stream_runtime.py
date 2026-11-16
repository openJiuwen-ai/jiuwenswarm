# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic business output behind the production resident Worker."""

import asyncio
import json
import os
from pathlib import Path

import diagnostic_worker


class InflightRuntime(diagnostic_worker.TrafficRuntime):
    async def dispatch_parsed_request(self, ws, request, send_lock):
        root = Path(os.environ["INFLIGHT_TEST_DIR"])
        with (root / "executions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"request_id": request.request_id, "pid": os.getpid()}) + "\n")
        for index in range(int(request.params.get("frames") or 40)):
            await ws.send(json.dumps({"request_id": request.request_id, "is_stream": True,
                                      "is_complete": False, "index": index}))
            if index in {4, 19}:
                (root / f"at-{index}").touch()
                async with asyncio.timeout(30):
                    while not (root / f"release-{index}").exists():
                        await asyncio.sleep(0.01)
        await ws.send(json.dumps({"request_id": request.request_id, "is_stream": True,
                                  "is_complete": True, "phase": "final"}))
        (root / "finished").touch()


def configure():
    diagnostic_worker.TrafficRuntime = InflightRuntime
    diagnostic_worker.configure("normal")
