# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validate connector configuration shipped inside the built-in MCP seed."""

import json
from pathlib import Path
from zipfile import ZipFile


def test_wecom_auth_uses_current_cli_subcommand() -> None:
    """Enterprise WeChat OAuth must use the current CLI ``auth init`` command."""
    seed = (
        Path(__file__).parents[3]
        / "jiuwenswarm"
        / "resources"
        / "agent"
        / "workspace"
        / "mcp_builtins_v0.2.zip"
    )
    with ZipFile(seed) as archive:
        config = json.loads(
            archive.read("mcp_builtins/wecom/cli.json").decode("utf-8")
        )

    assert config["auth"] == {
        "darwin": "wecom-cli auth init --noninteractive",
        "linux": "wecom-cli auth init --noninteractive",
        "win32": "wecom-cli.cmd auth init --noninteractive",
    }
