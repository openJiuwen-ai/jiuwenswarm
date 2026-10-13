# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime preparation must preserve deployment-owned AgentOS configuration."""

from pathlib import Path
import zipfile

import pytest
from ruamel.yaml import YAML

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common import utils as common_utils
from jiuwenswarm.common._build_config import VERSION


PACKAGE_ROOT = Path(common_utils.__file__).resolve().parent.parent


@pytest.fixture
def runtime_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package_root = tmp_path / "package"
    resources = package_root / "resources"
    template_workspace = resources / "agent" / "workspace"
    template_workspace.mkdir(parents=True)
    (resources / "config.yaml").write_bytes(
        (PACKAGE_ROOT / "resources" / "config.yaml").read_bytes()
    )
    (resources / ".env.template").write_text("", encoding="utf-8")
    with zipfile.ZipFile(template_workspace / "mcp_builtins_v0.2.zip", "w") as seed:
        seed.writestr("mcp_builtins/.mcp_builtins_version", "v0.2")

    workspace = tmp_path / "runtime"
    (workspace / "config").mkdir(parents=True)
    monkeypatch.setattr(common_utils, "_find_package_root", lambda: package_root)
    monkeypatch.setattr(common_utils, "get_user_workspace_dir", lambda: workspace)
    monkeypatch.setattr(common_utils, "_install_default_builtin_skills", lambda **_kwargs: None)
    monkeypatch.setattr(common_utils, "ensure_default_builtin_skills", lambda: None)
    return workspace


@pytest.mark.parametrize(
    ("mcp_state", "config_version"),
    [("ready", None), ("missing", "old-version"), ("outdated", "old-version")],
)
def test_agentos_runtime_preserves_yuanrong_config(
    runtime_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: str,
    config_version: str | None,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    deployment_template = (
        PACKAGE_ROOT.parent / "deploy" / "yuanrong" / "conf"
        / "gateway-config-yuanrong.template.yaml"
    )
    deployed = YAML().load(deployment_template.read_text(encoding="utf-8"))
    deployed["gateway"]["cron"]["store_backend"] = "etcd"
    deployed["gateway"]["cron"]["etcd_endpoints"] = "http://192.0.2.10:2379"
    deployed["gateway"]["cron"]["etcd_prefix"] = "/agentos/custom/cron/"
    if config_version is not None:
        deployed["config_version"] = config_version
    config_path = runtime_workspace / "config" / "config.yaml"
    config_module.dump_yaml_round_trip(config_path, deployed)
    original_config = config_path.read_bytes()

    installed = runtime_workspace / "agent" / "workspace" / "mcp" / "mcp_builtins"
    if mcp_state != "missing":
        installed.mkdir(parents=True)
        installed_version = "v0.2" if mcp_state == "ready" else "v0.1"
        (installed / ".mcp_builtins_version").write_text(installed_version, encoding="utf-8")

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    assert config_path.read_bytes() == original_config
    assert (installed / ".mcp_builtins_version").read_text(encoding="utf-8") == "v0.2"


def test_personal_runtime_still_migrates_config(
    runtime_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_PROFILE", raising=False)
    config_path = runtime_workspace / "config" / "config.yaml"
    config_path.write_text(
        "# operator configuration\n"
        "preferred_language: en  # keep this choice\n"
        "operator_extra:\n"
        "  nested: keep\n"
        "react:\n"
        "  answer_chunk_size: 300\n"
        "gateway:\n"
        "  cron:\n"
        "    store_backend: file\n"
        "  agent_client:\n"
        "    type: websocket\n",
        encoding="utf-8",
    )

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    migrated = config_module.load_yaml_round_trip(config_path)
    assert migrated["config_version"] == VERSION
    assert migrated["preferred_language"] == "en"
    assert migrated["react"]["answer_chunk_size"] == 300
    assert "context_engine_config" in migrated["react"]
    assert migrated["gateway"]["agent_client"]["type"] == "websocket"
    assert migrated["gateway"]["cron"]["store_backend"] == "file"
    assert migrated["operator_extra"]["nested"] == "keep"
    written = config_path.read_text(encoding="utf-8")
    assert "# operator configuration" in written
    assert "# keep this choice" in written
