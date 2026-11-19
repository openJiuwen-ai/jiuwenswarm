# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify JiuwenMemory SDK assembly and video tools in an isolated frozen backend."""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory


async def verify_memory_runtime(root: Path) -> None:
    """Assemble the real provider without sending model requests or ingesting media."""
    from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import AgentMemoryMemoryProvider
    from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import _build_security_provider

    for video in (False, True):
        profile_root = root / ("video" if video else "text")
        profile_root.mkdir()
        settings = {
            "watch_document": False,
            "llm_base_url": "http://127.0.0.1:9/v1",
            "llm_api_key": "bundle-check",
            "embedder_base_url": "http://127.0.0.1:9/v1",
            "embedder_api_key": "bundle-check",
        }
        if video:
            settings.update({
                "local_media_roots": [{
                    "scope": {"org": "local", "user": "bundle-check"},
                    "root": str(profile_root),
                }],
                "asr_base_url": "http://127.0.0.1:9/v1",
                "asr_model": "bundle-check",
                "asr_api_key": "bundle-check",
                "video_base_url": "http://127.0.0.1:9/v1",
                "video_model": "bundle-check",
                "video_api_key": "bundle-check",
            })
        provider = AgentMemoryMemoryProvider(
            user_id="bundle-check",
            security_provider=_build_security_provider("local", "bundle-check"),
            config_dict={
                "globals": settings,
                "markdown_store": {"default": {"target": "local", "params": {
                    "root": str(profile_root / "markdown"),
                }}},
            },
        )
        try:
            await provider.initialize(scope_id="local", session_id="bundle-check")
            if not provider.is_initialized:
                raise RuntimeError("JiuwenMemory SDK provider did not initialize")
            names = {schema["name"] for schema in provider.get_tool_schemas()}
            required = {"agent_memory_search", "agent_memory_conclude", "agent_memory_read_md_file"}
            if not required <= names:
                raise RuntimeError(f"Missing memory tools: {sorted(required - names)}")
            video_tools = {"agent_memory_video_submit", "agent_memory_video_status"}
            if video and not video_tools <= names:
                raise RuntimeError(f"Missing video memory tools: {sorted(video_tools - names)}")
            if not video and video_tools & names:
                raise RuntimeError("Video tools registered without a video profile")
        finally:
            await provider.shutdown()


def run_isolated(executable: Path) -> int:
    """Set private paths before the executable imports user configuration."""
    executable = executable.resolve()
    with TemporaryDirectory(prefix="memory-bundle-check-") as directory:
        root = Path(directory)
        (root / "config").mkdir()
        env = {}
        model_prefixes = ("JIUWEN_MEMORY_", "EMBED_")
        for key, value in os.environ.items():
            if not key.startswith(model_prefixes):
                env[key] = value
        env.update({
            "JIUWENSWARM_DATA_DIR": str(root),
            "JIUWENSWARM_HOME": str(root),
            "AGENTOS_GATEWAY_LOG_DIR": str(root / "logs"),
        })
        env.pop("JIUWENSWARM_RUNTIME_WORKSPACE_READY", None)
        result = subprocess.run(
            [str(executable), str(Path(__file__).resolve())],
            cwd=root,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
        if result.returncode:
            logging.error("Frozen JiuwenMemory verification failed (exit %s)", result.returncode)
            for log in sorted(root.rglob("*.log")):
                logging.error("%s", log.read_text(encoding="utf-8", errors="replace")[-16000:])
        return result.returncode


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if getattr(sys, "frozen", False):
        try:
            with TemporaryDirectory(prefix="memory-profile-check-") as directory:
                asyncio.run(verify_memory_runtime(Path(directory)))
        except Exception:
            # Third-party imports can replace stderr and the exception hook.
            # Preserve diagnostics for the launcher independently of logging.
            root = Path(os.environ.get("JIUWENSWARM_DATA_DIR", os.getcwd()))
            (root / "memory-bundle-check.log").write_text(traceback.format_exc(), encoding="utf-8")
            return 1
        logging.info("JiuwenMemory frozen SDK and video tool verification passed")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    return run_isolated(parser.parse_args().executable)


if __name__ == "__main__":
    raise SystemExit(main())
