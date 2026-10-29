# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compute Source identity outside the Front process and its event-loop GIL."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys


async def probe_identity(*, timeout: float = 30.0) -> tuple[str, dict | None]:
    from jiuwenswarm.server.supervisor.identity import code_identity, _identity_path

    if getattr(sys, "frozen", False) or _identity_path().is_file():
        return code_identity(), None
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "jiuwenswarm.server.supervisor.identity_probe",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode or len(stdout) > 8192:
        raise RuntimeError("Source identity helper failed")
    payload = json.loads(stdout)
    from jiuwenswarm.server.supervisor.identity import _valid_fingerprint

    package = os.path.normcase(str(Path(__file__).resolve().parents[2]))
    if payload.get("package") != package or not _valid_fingerprint(payload.get("code_identity")):
        raise RuntimeError("Source identity helper returned an invalid deployment")
    observation = payload.get("source_observation")
    return payload["code_identity"], observation if isinstance(observation, dict) else None


def main() -> None:
    from jiuwenswarm.server.supervisor.identity import code_identity, _identity_path
    from jiuwenswarm.server.supervisor.identity_cache import launch_source_observation

    fingerprint = code_identity()
    print(json.dumps({"package": os.path.normcase(str(Path(__file__).resolve().parents[2])),
                      "code_identity": fingerprint, "source_observation": None if _identity_path().is_file() else launch_source_observation()}))


if __name__ == "__main__":
    main()
