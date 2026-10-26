from __future__ import annotations

from pathlib import Path

from jiuwenswarm.server.im.im_hosting.cli_resolve import resolve_cli_path
from jiuwenswarm.server.im.im_hosting.paths import hosting_db_path, hosting_root_dir


def test_hosting_root_is_service_level_not_agent_workspace(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "jiuwenswarm.server.im.im_hosting.paths.get_user_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.im.im_hosting.paths.get_service_root_dir",
        lambda service_id="default": tmp_path / f"service_{service_id}",
    )
    monkeypatch.setattr("jiuwenswarm.server.im.im_hosting.paths.is_enterprise", lambda: False)
    root = hosting_root_dir()
    db = hosting_db_path()
    assert root == tmp_path / "service_default" / "im_hosting"
    assert db == root / "hosting.db"
    assert "jiuwenclaw_workspace" not in str(db)
    assert "agent_" not in str(db)


def test_resolve_cli_path_uses_which(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.im.im_hosting.cli_resolve.shutil.which",
        lambda name: r"C:\Users\admin\AppData\Roaming\npm\lark-cli.cmd" if name == "lark-cli" else None,
    )
    assert resolve_cli_path("feishu").endswith("lark-cli.cmd")
    assert resolve_cli_path("unknown") is None
