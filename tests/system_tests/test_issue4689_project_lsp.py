# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project leases against actual SDK-managed JSON-RPC server subprocesses."""
import json
import sys
from pathlib import Path
import asyncio

import psutil

import pytest

_SERVER = r'''
import json, os, sys
from pathlib import Path
with Path(sys.argv[1]).open("a") as file:
    file.write(json.dumps({"pid": os.getpid(), "cwd": os.getcwd()}) + "\n")
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise SystemExit(0)
        if line == b"\r\n":
            break
        name, value = line.decode().split(":", 1)
        headers[name.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    method = message.get("method")
    if method == "exit":
        break
    if "id" not in message:
        continue
    if method == "initialize":
        result = {"capabilities": {"documentSymbolProvider": True, "textDocumentSync": 1}}
    elif method == "textDocument/documentSymbol":
        result = [{"name": Path.cwd().name, "kind": 12,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
            "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}]
    else:
        result = None
    body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()
'''


@pytest.mark.asyncio
async def test_project_servers_reuse_within_project_and_close_at_worker_shutdown(tmp_path, monkeypatch):
    from jiuwenswarm.runtime import kernel
    from jiuwenswarm.runtime.project_lsp import install_project_lsp_leases
    from openjiuwen.harness.tools.lsp_tool import _tool

    script = tmp_path / "server.py"
    script.write_text(_SERVER, encoding="utf-8")
    starts = tmp_path / "starts.jsonl"
    config = {"lsp": {"custom_servers": {"pyright": {
        "command": sys.executable, "args": [str(script), str(starts)],
        "extensions": [".py"], "language_id": "python"}}}}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: config)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    owner = kernel.ResourceKernel()
    monkeypatch.setattr(kernel, "_PROCESS_KERNEL", owner)
    install_project_lsp_leases()
    try:
        for name in ("project-a", "project-a", "project-b"):
            project = tmp_path / name
            project.mkdir(exist_ok=True)
            (project / "pyproject.toml").write_text("[project]\nname = 'fixture'\nversion = '0.0.0'", encoding="utf-8")
            (project / "main.py").write_text("x", encoding="utf-8")
            result = await _tool.call_lsp_tool({"operation": "documentSymbol", "file_path": "main.py"},
                                              workspace=project)
            assert result["success"], result
            assert name in str(result["data"])
        rows = [json.loads(line) for line in starts.read_text().splitlines()]
        assert len(rows) == 2
        assert len({row["pid"] for row in rows}) == 2
        assert {Path(row["cwd"]).name for row in rows} == {"project-a", "project-b"}
        assert owner.resource_count == 2
        children = [psutil.Process(row["pid"]) for row in rows]
    finally:
        await owner.close()
    assert owner.resource_count == 0
    for child in children:
        try:
            await asyncio.to_thread(child.wait, timeout=5)
        except psutil.NoSuchProcess:
            pass
