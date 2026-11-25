"""Paper optimization queries backed by the durable RSI workspace."""

from pathlib import Path

from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.provider import (
    PaperArtifactProviderImpl,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.storage import TaskStorage


class WorkspacePaperArtifactProvider(PaperArtifactProviderImpl):
    """Resolve historical paper snapshots when the run is no longer in memory."""

    def __init__(self, tasks_root: str | Path) -> None:
        super().__init__()
        self._tasks_root = Path(tasks_root).expanduser().resolve()

    def _storage_for(self, task_id: str) -> TaskStorage:
        try:
            return super()._storage_for(task_id)
        except KeyError:
            if task_id in {"", ".", ".."} or Path(task_id).name != task_id:
                raise
            run_dir = (self._tasks_root / task_id / "run").resolve()
            if not run_dir.is_relative_to(self._tasks_root):
                raise
            storage = TaskStorage(run_dir)
            if not storage.state_path.is_file():
                raise
            return storage
