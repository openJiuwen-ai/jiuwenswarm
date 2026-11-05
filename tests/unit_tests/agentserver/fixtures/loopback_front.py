# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Front process whose abrupt exit must retire its attached Worker."""

import asyncio
import json
import os
from pathlib import Path
import sys

from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


async def main() -> None:
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness, stub=True, connect_timeout=8)
    try:
        await supervisor.run_until_ipc_ready()
        if not await readiness.wait_agent_ready(5):
            raise RuntimeError("Worker did not reach readiness")
        Path(sys.argv[1]).write_text(json.dumps({"front_pid": os.getpid(),
                                               "worker_pid": supervisor.worker_pid}))
        await asyncio.Event().wait()
    finally:
        await supervisor.stop()


if __name__ == "__main__":
    asyncio.run(main())
