"""jiuwenswarm-init 必须遵循 JIUWENSWARM_DATA_DIR（命名实例）。"""

from pathlib import Path

from jiuwenswarm import init_workspace
from jiuwenswarm.common import utils


def test_run_init_uses_data_dir_for_default_workspace(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "instance-workspace"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(target))
    monkeypatch.setattr(utils, "_workspace_base_dir", None)
    monkeypatch.setattr(utils, "_user_home", tmp_path / "home")
    monkeypatch.setattr(utils, "_initialized", False)

    captured: dict[str, object] = {}

    def fake_init_user_workspace(*, overwrite: bool, workspace_dir: Path):
        captured["workspace_dir"] = Path(workspace_dir)
        captured["overwrite"] = overwrite
        return str(workspace_dir)

    monkeypatch.setattr(init_workspace, "init_user_workspace", fake_init_user_workspace)

    assert init_workspace.run_init(force=False) == 0
    assert captured["workspace_dir"] == target
    assert captured["overwrite"] is False
