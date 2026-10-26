from __future__ import annotations

import os
from pathlib import Path

import pytest

from openjiuwen.harness.personal_context.models import RawChangeItem
from openjiuwen.harness.personal_context.source_metadata import upsert_source_metadata

from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI


def _write_source(home: Path) -> tuple[str, Path]:
    source_root = home / "workspace" / "source-meta"
    source_id = upsert_source_metadata(
        source_root,
        RawChangeItem(
            logical_id="github:pull:42",
            revision_id="revision-1",
            operation="upsert",
            title="GitHub PR 42",
            content="source body must not be persisted",
            original_ref="https://github.com/openjiuwen/agent-core/pull/42",
            metadata={"resource": "pull_request"},
        ),
        provider="github",
        service_id="github-main",
        observed_at="2026-08-12T00:00:00Z",
    )
    return source_id, source_root / f"{source_id}.md"


def _link(page: Path, target: Path) -> str:
    relative = os.path.relpath(target, start=page.parent).replace("\\", "/")
    return f"[来源1]({relative})"


@pytest.mark.asyncio
async def test_host_graph_and_tree_are_empty_without_context(tmp_path: Path) -> None:
    host = PersonalContextHostAPI(home=tmp_path / "personal_context")

    assert await host.get_graph() == {"context_ready": False, "nodes": [], "edges": []}
    assert await host.get_tree() == {"context_ready": False, "nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_host_delegates_breadth_first_graph_and_tree_slices(
    tmp_path: Path,
) -> None:
    home = tmp_path / "personal_context"
    context = home / "workspace" / "context"
    page = context / "topics" / "agent.md"
    page.parent.mkdir(parents=True)
    source_id, source_path = _write_source(home)
    (context / "description.md").write_text(
        "# Context\n\n[Topics](topics/description.md)\n",
        encoding="utf-8",
    )
    (context / "topics" / "description.md").write_text(
        "# Topics\n\n[Agent](agent.md)\n",
        encoding="utf-8",
    )
    page.write_text("# Agent\n\n" + _link(page, source_path) + "\n", encoding="utf-8")
    host = PersonalContextHostAPI(home=home)

    graph = await host.get_graph(root_id=None, depth=2)
    tree = await host.get_tree(root_id="page:topics/description.md", depth=1)

    assert [node["id"] for node in graph["nodes"]] == [
        "page:description.md",
        "page:topics/description.md",
    ]
    assert [node["id"] for node in tree["nodes"]] == ["page:topics/agent.md"]
    assert all(
        not str(node["id"]).startswith("source:")
        for node in graph["nodes"] + tree["nodes"]
    )
    assert all(source_id not in str(edge) for edge in graph["edges"] + tree["edges"])


@pytest.mark.asyncio
async def test_host_exposes_source_detail_separately(tmp_path: Path) -> None:
    home = tmp_path / "personal_context"
    source_id, _source_path = _write_source(home)

    detail = await PersonalContextHostAPI(home=home).get_source(source_id)

    assert detail["source_id"] == source_id
    assert detail["title"] == "GitHub PR 42"
    assert detail["service_id"] == "github-main"
    assert "markdown" not in detail
