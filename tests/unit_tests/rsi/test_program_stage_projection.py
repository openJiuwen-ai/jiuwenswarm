from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import provider_node_to_dict
from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector
from openjiuwen.rsi.artifact_rsi.program_opt import events
from openjiuwen.rsi.artifact_rsi.program_opt.state import ProgramRunState, read_tree_file


def test_program_stages_and_best_branch_survive_snapshot_refresh(tmp_path):
    run = ProgramRunState(task_id="t", run_dir=tmp_path / "engine", total_iterations=2)
    projector = RsiProjector(tmp_path / "web")
    def emit(event):
        for message in run.absorb(event):
            if message.event_type == "node":
                projector.on_provider_node("t", message.node)
            elif message.event_type == "node.stage":
                projector.on_node_stage("t", {"node_ref": message.node_ref, "stage": message.stage})
    emit(events.seeded(0, 0.1))
    for iteration, score in ((1, 0.2), (2, 0.3)):
        emit({"type": "candidate_started", "iteration": iteration, "parentIndex": 0})
        emit({"type": "stage", "iteration": iteration, "id": "evaluate", "name": "正在评测程序"})
        tree = projector.sync_provider_tree("t", read_tree_file("t"))
        assert tree["nodes"][-1]["type"] == "PROVISIONAL"
        assert tree["nodes"][-1]["description"] == "正在评测程序"
        assert tree["iteration"] == iteration - 1
        emit(events.expanded(iteration, 0, 1, score, True, iteration=iteration))
        assert provider_node_to_dict(run.nodes[iteration])["type"] == "PROVISIONAL"
        emit(events.merged(iteration, True, "better"))
    tree = projector.sync_provider_tree("t", read_tree_file("t"))
    assert len(tree["nodes"]) == 3
    assert tree["iteration"] == 2
    assert tree["nodes"][1]["type"] == "REJECTED"
    assert tree["nodes"][1]["extra"]["program"]["logical_kind"] == "adopted"
    assert tree["nodes"][2]["type"] == "ADOPTED"
