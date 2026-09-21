import pytest

from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.orchestrator import PaperTreeOrchestrator
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.schemas import (
    NodeStageEvent,
    PaperNodeExtra,
    RsiTreeNode,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.storage import TaskStorage


@pytest.mark.asyncio
async def test_stage_snapshot_keeps_one_pending_node_and_zero_completed(tmp_path):
    storage = TaskStorage(str(tmp_path / "provider"))
    node = RsiTreeNode(
        node_id="artifact:t:node:1", iteration=1, parent_id=None,
        type="reporting", adopted=False,
        extra={"paper": {"outcome": "pending"}},
    )
    storage.append_node(node)
    orchestrator = object.__new__(PaperTreeOrchestrator)
    orchestrator.storage = storage
    orchestrator.on_event = None
    projector = RsiProjector(tmp_path / "web")
    for stage in ("正在调研文献", "正在执行实验", "正在撰写论文"):
        await orchestrator._emit(NodeStageEvent(
            node_ref=node.node_id, stage={"id": stage, "name": stage},
        ))
        tree = projector.sync_provider_tree("t", {
            "nodes": storage.load_tree(), "depth": 0, "iteration": 0,
        })
        assert len(tree["nodes"]) == 2
        pending = next(item for item in tree["nodes"] if item["node_id"] != "ROOT")
        assert pending["description"] == stage
        assert pending["type"] == "PROVISIONAL"
        assert pending["extra"]["stage"]["name"] == stage
        assert projector.derive_progress("t")["iteration"] == 0
    completed = storage.load_tree()[0].model_copy(update={
        "adopted": True, "extra": {"paper": {"outcome": "success"}},
    })
    storage.append_node(completed)
    tree = projector.sync_provider_tree("t", {
        "nodes": storage.load_tree(), "depth": 0, "iteration": 1,
    })
    assert len(tree["nodes"]) == 2
    pending = next(item for item in tree["nodes"] if item["node_id"] != "ROOT")
    assert pending["type"] == "ADOPTED"
    assert projector.derive_progress("t")["iteration"] == 1


def test_pruned_provider_node_replaces_pending_projection_and_keeps_reason(tmp_path):
    projector = RsiProjector(tmp_path / "web")
    projector.sync_provider_tree(
        "t",
        {
            "nodes": [
                RsiTreeNode(
                    node_id="artifact:t:node:1",
                    iteration=1,
                    parent_id=None,
                    type="reporting",
                    adopted=False,
                    extra={"paper": {"outcome": "pending"}},
                )
            ],
            "depth": 0,
            "iteration": 0,
        },
    )

    pruned = RsiTreeNode(
        node_id="artifact:t:node:1",
        iteration=1,
        parent_id=None,
        type="pruned",
        adopted=False,
        reason="资料获取质量不佳，已剪枝。",
        failure_class="pipeline_failed",
        extra={
            "paper": PaperNodeExtra(
                logical_kind="pruned",
                round_index=1,
                attempt=1,
                outcome="failed",
                node_run_id="t-r1",
            ).model_dump(mode="json")
        },
    )
    tree = projector.sync_provider_tree(
        "t", {"nodes": [pruned], "depth": 1, "iteration": 1}
    )

    node = next(item for item in tree["nodes"] if item["node_id"] != "ROOT")
    assert node["type"] == "PRUNED"
    assert node["failure_reason"] == "资料获取质量不佳，已剪枝。"
