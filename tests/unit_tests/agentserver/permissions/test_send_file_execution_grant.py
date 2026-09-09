"""Exact current-task send grant contract without historical registries."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
    clear_send_file_execution_grant,
    consume_send_file_execution_grant,
    create_send_file_execution_grant,
    current_send_file_execution_grant,
    publish_send_file_execution_grant,
)


@pytest.fixture(autouse=True)
def _clear_grant() -> None:
    clear_send_file_execution_grant()
    yield
    clear_send_file_execution_grant()


def test_no_grant_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="grant_missing"):
        consume_send_file_execution_grant((tmp_path / "report.md",))


def test_exact_path_and_normalized_targets_consume_once(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    grant = create_send_file_execution_grant(
        (path,),
        target_channels=["web", "web", " feishu "],
    )
    publish_send_file_execution_grant(grant)

    items = consume_send_file_execution_grant(
        (path,),
        target_channels='["feishu", "web"]',
    )

    assert tuple(item.resolved_path for item in items) == (path.resolve(),)
    assert current_send_file_execution_grant() is None
    with pytest.raises(ValueError, match="grant_missing"):
        consume_send_file_execution_grant((path,))


@pytest.mark.parametrize("change", ["path", "target"])
def test_mismatch_clears_grant(tmp_path: Path, change: str) -> None:
    path = tmp_path / "report.md"
    publish_send_file_execution_grant(
        create_send_file_execution_grant((path,), target_channels=("web",))
    )

    with pytest.raises(ValueError, match="grant_mismatch"):
        consume_send_file_execution_grant(
            ((tmp_path / "other.md") if change == "path" else path,),
            target_channels=("feishu",) if change == "target" else ("web",),
        )

    assert current_send_file_execution_grant() is None


def test_grant_authorizes_path_not_content(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    path.write_text("approval-time", encoding="utf-8")
    publish_send_file_execution_grant(create_send_file_execution_grant((path,)))
    path.write_text("delivery-time", encoding="utf-8")

    items = consume_send_file_execution_grant((path,))

    assert items[0].resolved_path.read_text(encoding="utf-8") == "delivery-time"


@pytest.mark.asyncio
async def test_copied_async_contexts_share_one_terminal_grant(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    publish_send_file_execution_grant(create_send_file_execution_grant((path,)))
    ready = asyncio.Event()

    async def consume() -> str:
        await ready.wait()
        try:
            consume_send_file_execution_grant((path,))
        except ValueError as exc:
            return str(exc)
        return "consumed"

    consumers = [asyncio.create_task(consume()) for _ in range(2)]
    ready.set()

    assert sorted(await asyncio.gather(*consumers)) == [
        "consumed",
        "send_file_execution_grant_missing",
    ]
    assert current_send_file_execution_grant() is None
