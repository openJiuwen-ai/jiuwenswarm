"""jiuwenswarm-init 必须遵循 JIUWENSWARM_DATA_DIR（命名实例）。"""

from pathlib import Path

from jiuwenswarm import init_workspace
from jiuwenswarm.common import paths, utils


def test_run_init_force_with_data_dir_skips_default_instance_check(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "instance-workspace"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(target))
    monkeypatch.setattr(paths, "_workspace_base_dir", None)
    # home 与解析缓存已收敛进 common.paths（utils 仅转发），patch 点随之迁移
    monkeypatch.setattr(paths, "_user_home", tmp_path / "home")
    monkeypatch.setattr(paths, "_initialized", False)

    def fail_default_status():
        raise AssertionError("default instance status must not be checked for DATA_DIR init")

    monkeypatch.setattr(init_workspace, "get_default_instance_status", fail_default_status)
    monkeypatch.setattr(
        init_workspace,
        "init_user_workspace",
        lambda *, overwrite, workspace_dir: str(workspace_dir),
    )

    assert init_workspace.run_init(force=True) == 0


def test_run_init_uses_data_dir_for_default_workspace(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "instance-workspace"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(target))
    monkeypatch.setattr(paths, "_workspace_base_dir", None)
    monkeypatch.setattr(paths, "_user_home", tmp_path / "home")
    monkeypatch.setattr(paths, "_initialized", False)

    captured: dict[str, object] = {}

    def fake_init_user_workspace(*, overwrite: bool, workspace_dir: Path):
        captured["workspace_dir"] = Path(workspace_dir)
        captured["overwrite"] = overwrite
        return str(workspace_dir)

    monkeypatch.setattr(init_workspace, "init_user_workspace", fake_init_user_workspace)

    assert init_workspace.run_init(force=False) == 0
    assert captured["workspace_dir"] == target
    assert captured["overwrite"] is False
