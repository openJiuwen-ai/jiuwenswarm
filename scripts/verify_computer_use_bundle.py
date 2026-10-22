# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the installed SDK, embedded REPL and native handshake."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import tempfile

from jiuwenswarm.agents.harness.common.cua_defaults import build_cua_agent_card
from jiuwenswarm.agents.harness.common.cua_factory import build_cua_agent_config
from jiuwenswarm.agents.harness.common.rails.cua_extension_rail import CuaExtensionRail
from jiuwenswarm.cua_engine.hosting import engine_root, prepare_native
from jiuwenswarm.cua_engine.runtime import ReplRuntime


LOGGER = logging.getLogger(__name__)
PROBE = """
import json
from computer_use.client.connection import Connection
from computer_use.paths import native_driver
connection = Connection(lambda *args: None)
try:
    hello = await connection.connect()
    print(json.dumps({'platform': hello['platform'], 'driver': native_driver()['command']}))
finally:
    connection.close()
    await connection.wait_closed()
packaged_value = 41
"""


async def verify_computer_use_bundle() -> None:
    """Verify without a compiler, external Python, desktop input or permission prompts."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Computer Use bundle verification must run inside the frozen executable")
    if sys.version_info < (3, 11):
        raise RuntimeError("The installed cua_repl requires a bundled Python 3.11+ runtime")
    spec = build_cua_agent_config(None, card=build_cua_agent_card())
    if not any(isinstance(rail, CuaExtensionRail) for rail in spec.rails or []):
        raise RuntimeError("The desktop installer did not enable its bundled CUA extension")
    executable = prepare_native()
    skill_name = "computer-use-macos" if sys.platform == "darwin" else "computer-use-windows"
    skill = engine_root() / "skills" / skill_name / "SKILL.md"
    if "## API surface" not in skill.read_text(encoding="utf-8"):
        raise RuntimeError("The installed Computer Use Skill is missing its API surface")
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-cua-verify-") as temporary:
        env = dict(os.environ, PATH=temporary, PYTHONPATH="", JIUWENSWARM_DATA_DIR=temporary,
                   COMPUTER_USE_LOG_DIR=str(Path(temporary) / "logs"))
        env.pop("PYTHONHOME", None)
        previous_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        runtime = ReplRuntime()
        try:
            result = await runtime.execute(PROBE)
            if result.isError:
                raise RuntimeError("Packaged Computer Use handshake failed: " + _text(result))
            observed = json.loads(_text(result).strip())
            if Path(observed["driver"]).resolve() != executable.resolve():
                raise RuntimeError("The frozen SDK selected a driver outside its installed bundle")
            expected_platform = "macos" if sys.platform == "darwin" else "windows"
            if observed["platform"] != expected_platform:
                raise RuntimeError("The installed Computer Use driver has the wrong platform")
            result = await runtime.execute("print(packaged_value + 1)")
            if result.isError or _text(result).strip() != "42":
                raise RuntimeError("The installed Computer Use REPL did not retain Python variables")
        finally:
            try:
                await runtime.close()
            finally:
                os.environ.clear()
                os.environ.update(previous_env)
        if runtime.cleanup_error:
            raise RuntimeError("Computer Use cleanup failed: " + runtime.cleanup_error)


def _text(result) -> str:
    return "\n".join(part.text for part in result.content if part.type == "text")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(verify_computer_use_bundle())
    LOGGER.info("Computer Use frozen bundle verification passed")
