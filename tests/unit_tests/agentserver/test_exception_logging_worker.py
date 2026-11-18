# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise exception logging through the real Worker and native IPC."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from jiuwenswarm.server.ipc.protocol import KIND_HEARTBEAT
from tests.unit_tests.agentserver.test_ipc_backpressure import _gateway, _request, _worker


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["resident", "front_loopback"])
@pytest.mark.parametrize("bad_notes", [False, True])
async def test_real_worker_exception_logging_avoids_slow_source_cache(tmp_path, monkeypatch, transport, bad_notes):
    from websockets.asyncio.client import connect

    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", transport)
    async with _worker("exception-logging", tmp_path, monkeypatch) as (supervisor, process):
        pid = supervisor.worker_pid
        async with _gateway(supervisor) as (url, gateway, attached):
            async with connect(url, proxy=None) as client:
                await asyncio.wait_for(attached.wait(), 2)
                request = _request(1)
                request.params["bad_notes"] = bad_notes
                dispatch = asyncio.create_task(supervisor.dispatch_parsed_request(
                    gateway["ws"], request, gateway["lock"],
                ))
                try:
                    async with asyncio.timeout(5):
                        while True:
                            final = json.loads(await client.recv())
                            if final.get("request_id") == request.request_id:
                                break
                            # Readiness can push a model-list notification first.
                            assert final.get("metadata", {}).get("_jiuwenswarm_server_push") is True, final
                            assert final.get("body", {}).get("event_type") == "models.updated", final
                    await asyncio.wait_for(dispatch, 3)
                    evidence = {**final, "transport": transport, "bad_notes": bad_notes, "pid": pid}
                    (tmp_path / "acceptance.json").write_text(
                        json.dumps(evidence, indent=2), encoding="utf-8",
                    )
                    assert final.get("phase") == "final", final
                    assert final["source_lookups"] == 0
                    assert final["log_seconds"] < 1
                    assert final["redacted"] and final["traceback_present"]
                    if bad_notes:
                        assert final["notes_omitted"]
                    expected = Path(__file__).resolve().parents[3] / "jiuwenswarm/common/utils.py"
                    assert Path(final["utils_origin"]).resolve() == expected

                    # Observe both a fresh transport pong and a fresh business-loop beat.
                    previous_beat = supervisor._worker_last_beat
                    await asyncio.sleep(1.1)
                    heartbeat_started = time.monotonic()
                    await supervisor._conn.send({"kind": KIND_HEARTBEAT})
                    async with asyncio.timeout(3):
                        while supervisor._worker_last_beat <= previous_beat or supervisor._last_rx < heartbeat_started:
                            await asyncio.sleep(0.01)
                    assert supervisor._last_rx >= heartbeat_started
                    assert supervisor.worker_pid == pid and process.returncode is None
                    assert supervisor._restart_count == 0
                    (tmp_path / "acceptance.json").write_text(json.dumps({
                        **evidence, "restarts": 0,
                        "transport_and_business_heartbeat": True,
                    }, indent=2), encoding="utf-8")
                finally:
                    dispatch.cancel()
                    await asyncio.gather(dispatch, return_exceptions=True)
    assert process.returncode == 0
