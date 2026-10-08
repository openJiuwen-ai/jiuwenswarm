"""Audited async gateway to the vendored dynamic-memory-cli contract."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .evidence import EvidenceWriter, write_json_atomic


VENDORED_SKILL = (
    Path(__file__).resolve().parents[5]
    / "resources"
    / "agent"
    / "workspace"
    / "skills"
    / "dynamic-memory-cli"
)


class DynamicMemoryGateway:
    """One formal memory state machine; callers never bypass this process."""

    def __init__(
        self,
        root: Path,
        evidence: EvidenceWriter,
        *,
        script: Path | None = None,
    ) -> None:
        self.root = root
        self.evidence = evidence
        self.script = script or VENDORED_SKILL / "scripts" / "dynamic_memory_cli.py"
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            if not self.script.is_file():
                raise FileNotFoundError(f"vendored dynamic-memory-cli not found: {self.script}")
            if not (self.root / "memory.sqlite3").is_file():
                await self._invoke("init", "--path", str(self.root), include_root=False)
            self._initialized = True

    async def call(self, *args: str) -> dict[str, Any]:
        await self.ensure_initialized()
        return await self._invoke(*args, include_root=True)

    async def _invoke(self, *args: str, include_root: bool) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(self.script)]
        if include_root:
            command.extend(("--root", str(self.root)))
        command.extend(args)
        env = dict(os.environ)
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        started = asyncio.get_running_loop().time()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000
        text = stdout.decode("utf-8", errors="replace")
        error_text = stderr.decode("utf-8", errors="replace")
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            result = {"error": "invalid_json", "stdout": text, "stderr": error_text}
            await self.evidence.append_audit(
                "memory-cli-calls",
                {"args": list(args), "returncode": process.returncode, "elapsed_ms": elapsed_ms, "result": result},
            )
            raise RuntimeError(f"dynamic-memory-cli returned invalid JSON: {text or error_text}") from exc
        await self.evidence.append_audit(
            "memory-cli-calls",
            {"args": list(args), "returncode": process.returncode, "elapsed_ms": elapsed_ms, "result": result},
        )
        if process.returncode != 0:
            raise RuntimeError(f"dynamic-memory-cli failed: {result}")
        return result

    async def file_command(self, command: str, payload: dict[str, Any], stem: str) -> dict[str, Any]:
        jobs = self.root / "jobs"
        path = jobs / f"{stem}-{uuid.uuid4().hex}.json"
        await asyncio.to_thread(write_json_atomic, path, payload)
        return await self.call(command, "--file", str(path))

    async def search(self, query: str) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {"query": "", "matches": []}
        # This exact CLI path merges published Pending and Built UTs.
        return await self.call("search", query)

    async def projection(self) -> dict[str, Any]:
        return await self.call("get-state")


__all__ = ["DynamicMemoryGateway", "VENDORED_SKILL"]
