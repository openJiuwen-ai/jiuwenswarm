# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exact send grant consumption and stable asset integration."""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
    clear_send_file_execution_grant,
    create_send_file_execution_grant,
    current_send_file_execution_grant,
    publish_send_file_execution_grant,
)
from jiuwenswarm.agents.harness.common.tools import send_file_to_user as send_module
from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit
from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
    VerifiedDownloadAssetOwner,
)
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)


@pytest.fixture(autouse=True)
def _clear_runtime_state() -> None:
    clear_send_file_execution_grant()
    send_module._SENT_FILE_PATHS_BY_SESSION.clear()
    yield
    clear_send_file_execution_grant()
    send_module._SENT_FILE_PATHS_BY_SESSION.clear()


def _toolkit(owner: VerifiedDownloadAssetOwner) -> SendFileToolkit:
    return SendFileToolkit(
        request_id="request-a",
        session_id="session-a",
        channel_id="web",
        metadata={"origin": "request-a"},
        require_execution_authorization=True,
        asset_owner=owner,
    )


def _delivery_patches(owner: VerifiedDownloadAssetOwner):
    server = MagicMock()
    server.send_push = AsyncMock()
    history = MagicMock()
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    return server, history, (
        patch.object(send_module, "send_runtime_push", server.send_push),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
            history,
        ),
        patch.object(WebFileDownloadManager, "_instance", manager),
    )


def test_consumer_accepts_only_exact_paths_and_targets() -> None:
    parameters = inspect.signature(
        SendFileToolkit._consume_execution_authorization
    ).parameters

    assert set(parameters) == {"requested_paths", "target_channels"}


@pytest.mark.asyncio
async def test_authorized_send_captures_delivery_time_bytes(tmp_path: Path) -> None:
    source = tmp_path / "report.md"
    source.write_text("approval-time", encoding="utf-8")
    owner = VerifiedDownloadAssetOwner(
        root=tmp_path / "assets",
        start_sweeper=False,
    )
    publish_send_file_execution_grant(
        create_send_file_execution_grant((source,), target_channels=("web",))
    )
    source.write_text("delivery-time", encoding="utf-8")
    server, history, patches = _delivery_patches(owner)
    loop_thread_id = threading.get_ident()
    stage_thread_ids: list[int] = []
    stage = owner.stage

    def _stage_in_worker(*args, **kwargs):
        stage_thread_ids.append(threading.get_ident())
        return stage(*args, **kwargs)

    with patches[0], patches[1], patches[2], patch.object(
        owner,
        "stage",
        side_effect=_stage_in_worker,
    ):
        result = await _toolkit(owner).send_file(
            source.as_posix(),
            target_channels=("web",),
        )

    assert result == "成功发送 1 个文件"
    assert stage_thread_ids and all(
        thread_id != loop_thread_id for thread_id in stage_thread_ids
    )
    assert current_send_file_execution_grant() is None
    payload = server.send_push.await_args.args[0]["payload"]["files"][0]
    assert payload["path"] != source.as_posix()
    assert Path(payload["path"]).read_text(encoding="utf-8") == "delivery-time"
    assert history.call_args.kwargs["extra"]["files"][0] == payload
    sidecar = next((tmp_path / "assets").glob("*.json"))
    assert json.loads(sidecar.read_text(encoding="utf-8"))["state"] == "committed"


@pytest.mark.asyncio
async def test_missing_grant_fails_before_asset_capture(tmp_path: Path) -> None:
    source = tmp_path / "report.md"
    source.write_text("content", encoding="utf-8")
    owner = VerifiedDownloadAssetOwner(
        root=tmp_path / "assets",
        start_sweeper=False,
    )

    result = await _toolkit(owner).send_file(source.as_posix())

    assert "send_file_execution_grant_missing" in result
    assert not (tmp_path / "assets").exists()


@pytest.mark.asyncio
async def test_mismatch_consumes_grant_and_replay_fails(tmp_path: Path) -> None:
    source = tmp_path / "report.md"
    source.write_text("content", encoding="utf-8")
    owner = VerifiedDownloadAssetOwner(
        root=tmp_path / "assets",
        start_sweeper=False,
    )
    publish_send_file_execution_grant(
        create_send_file_execution_grant((source,), target_channels=("web",))
    )
    toolkit = _toolkit(owner)

    mismatch = await toolkit.send_file(
        source.as_posix(),
        target_channels=("feishu",),
    )
    replay = await toolkit.send_file(
        source.as_posix(),
        target_channels=("web",),
    )

    assert "send_file_execution_grant_mismatch" in mismatch
    assert "send_file_execution_grant_missing" in replay
    assert current_send_file_execution_grant() is None
