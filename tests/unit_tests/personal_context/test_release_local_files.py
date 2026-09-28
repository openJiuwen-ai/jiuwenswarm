"""The pinned Core release must tolerate empty local-file candidates."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openjiuwen.harness.personal_context.config import PersonalContextFetchServiceConfig
from openjiuwen.harness.personal_context.fetch.local_files import LocalFilesFetchService


@pytest.mark.parametrize("with_normal_file", [True, False])
def test_pinned_core_skips_empty_local_file_and_git_directory(
    tmp_path: Path, with_normal_file: bool
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    if with_normal_file:
        (root / "note.md").write_text("usable knowledge", encoding="utf-8")
    (root / "empty.txt").write_text("  \n", encoding="utf-8")
    git_dir = root / ".git"
    git_dir.mkdir()
    (git_dir / "ignored.md").write_text("not knowledge", encoding="utf-8")
    config = PersonalContextFetchServiceConfig(
        service_id="notes",
        provider="local_files",
        enabled=True,
        interval_seconds=60,
        max_items_per_run=5,
        time_range={"mode": "all"},
        source={"root_dir": str(root)},
        credentials={},
    )
    service = LocalFilesFetchService(config, home=tmp_path / "home")

    async def collect() -> tuple[tuple[dict[str, object], ...], list[object]]:
        candidates = await service.prepare_run(
            run_id="run-1",
            run_started_at=datetime.now(UTC) + timedelta(seconds=1),
            cursor=None,
        )
        batches = [
            batch
            async for batch in service.fetch(
                run_id="run-1", cursor=None, candidates=candidates
            )
        ]
        return candidates, batches

    candidates, batches = asyncio.run(collect())

    expected_candidates = {"empty.txt", "note.md"} if with_normal_file else {"empty.txt"}
    assert {str(item["relative_path"]) for item in candidates} == expected_candidates
    assert len(batches) == 1
    assert [item.title for item in batches[0].items] == (["note.md"] if with_normal_file else [])
    assert len(batches[0].skipped_offsets) == 1
    assert batches[0].failures == ()
