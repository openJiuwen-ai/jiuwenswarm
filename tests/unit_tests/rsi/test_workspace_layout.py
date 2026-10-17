from pathlib import Path

from jiuwenswarm.agents.harness.common.rsi.context import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask, TaskStatus, utcnow_iso


def test_default_rsi_context_uses_legacy_rsi_tasks_root(monkeypatch, tmp_path: Path):
    user_workspace = tmp_path / ".jiuwenswarm"
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: user_workspace,
    )

    context = build_rsi_service_context(
        None,
        enable_harness_materialization=False,
    )

    expected_root = (user_workspace / "rsi" / "tasks").resolve()
    assert context.tasks_root == expected_root
    assert context.harness_activation_store.root == expected_root
    assert context.harness_activation_store.activation_path == expected_root / "activation.json"

    task_id = "rsi-layout-test"
    context.store.create(
        RsiTask(
            task_id=task_id,
            name="layout",
            scenario="HARNESS",
            status=TaskStatus.CREATED.value,
            created_at=utcnow_iso(),
            input_file=str(context.tasks_root / task_id / "input" / "dataset.json"),
            run_dir=str(context.tasks_root / task_id / "run"),
        )
    )
    context.ensure_root(task_id)

    task_root = context.tasks_root / task_id
    assert (task_root / "task.json").is_file()
    assert (task_root / "tree.json").is_file()
