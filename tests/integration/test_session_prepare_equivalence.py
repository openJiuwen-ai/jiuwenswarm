# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A session built by session.prepare equals one built by the chat path."""
# 在子进程里真实构建 DeepAgent（WarmupModelClient，不发 HTTP）：进程级工作区缓存指向 tmp_path，不碰真实 ~/.jiuwenswarm。

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD_SCRIPT = r'''
import asyncio, json, sys

MODE = sys.argv[1]


async def main():
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime import session_prepare_tracker
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import ensure_persistent_checkpointer
    from jiuwenswarm.server.runtime.prewarm import _build_warmup_config_base

    await ensure_persistent_checkpointer()
    cfg = _build_warmup_config_base()
    for entry in cfg.get("models", {}).get("defaults") or []:
        entry.setdefault("model_client_config", {}).update(
            api_key="test-key", api_base="http://127.0.0.1:9", client_provider="warmup",
        )
    facade = JiuWenSwarm()
    await facade.create_instance({}, mode=MODE, config_base=cfg)

    def request(sid, method):
        return AgentRequest(
            request_id=f"{method.value}-{sid}", channel_id="officeclaw", session_id=sid,
            agent_id="default", chat_id="officeclaw_chat_1", thread_id="thread-1",
            req_method=method, params={"mode": MODE, "query": "hi"},
            metadata={"user_id": "officeclaw_user_1"},
        )

    session_prepare_tracker.mark("sess_prepared", "building")
    await facade.prepare_session(
        session_id="sess_prepared", channel_id="officeclaw", mode=MODE, adapter_mode=MODE,
        request=request("sess_prepared", ReqMethod.SESSION_PREPARE),
    )
    deep = facade._adapter
    chat = request("sess_direct", ReqMethod.CHAT_SEND)
    tenant_tokens, mem_token = facade._bind_tenant_request_context(chat)
    try:
        direct = await deep._get_or_create_session_adapter(
            "sess_direct", request=chat, history_before_request_id=chat.request_id,
        )
    finally:
        facade._reset_tenant_request_context(tenant_tokens, mem_token)
    await direct.configure_session_runtime(session_id="sess_direct", channel_id="officeclaw", mode=MODE)

    def snapshot(sid):
        child = deep._session_adapters[sid]
        inst = child._instance
        norm = lambda value: str(value).replace(sid, "<sid>")
        return {
            "adapter": type(child).__name__,
            "rails": sorted(type(r).__name__ for r in inst._registered_rails),
            "tools": sorted(norm(k) for k in inst.ability_manager._tools),
            "thread_id": child._parent_thread_id,
            "workspace_dir": str(child._workspace_dir),
            "last_mode": child._last_mode,
            "permissions_body": norm(child._agent_permissions_body),
            "permission_routing": norm(getattr(child, "_permission_routing", None)),
            "model": type(child._model).__name__,
            "model_config_source": norm(getattr(child, "_model_config_source", None)),
        }

    print("SNAPSHOT " + json.dumps({"prepared": snapshot("sess_prepared"), "direct": snapshot("sess_direct")}))
    await facade.cleanup()


asyncio.run(main())
'''


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["agent", "flash"])
def test_prepared_session_matches_chat_built_session(tmp_path, mode):
    # An existing <data>/config makes it the config dir, so runtime_state files
    # land under tmp_path instead of the package resources.
    config_dir = tmp_path / "data" / "config"
    config_dir.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "jiuwenswarm" / "resources" / "config.yaml", config_dir / "config.yaml")
    env = dict(os.environ)
    env.update(
        JIUWENSWARM_DATA_DIR=str(tmp_path / "data"),
        API_KEY="test-key",
        API_BASE="http://127.0.0.1:9",
        MODEL_NAME="test-model",
        PYTHONPATH=str(REPO_ROOT),
        PYTHONUTF8="1",
    )
    env.pop("JIUWENCLAW_DATA_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-c", _BUILD_SCRIPT, mode],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300, check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("SNAPSHOT ")]
    assert proc.returncode == 0 and lines, proc.stderr[-3000:]
    built = json.loads(lines[-1][len("SNAPSHOT "):])
    prepared, direct = built["prepared"], built["direct"]

    assert prepared["rails"], "no rails registered; the build did not really run"
    assert prepared["thread_id"] == "thread-1"
    for field in prepared:
        assert prepared[field] == direct[field], field
