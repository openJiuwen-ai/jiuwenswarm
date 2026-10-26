# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Configuration for the SSH server channel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_config_dir


@dataclass
class ProxyConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 2222
    host_key_path: str = ""

    def __post_init__(self) -> None:
        if self.host_key_path:
            self.host_key_path = str(Path(self.host_key_path).expanduser())
        else:
            self.host_key_path = str(get_config_dir() / "ssh_host_key")


def proxy_config_from_dict(raw: dict[str, Any]) -> ProxyConfig:
    """Build proxy runtime config from channels.ssh YAML block."""
    return ProxyConfig(
        listen_host=str(raw.get("listen_host", "0.0.0.0")),
        listen_port=int(raw.get("listen_port", 2222)),
        host_key_path=str(raw.get("host_key_path") or ""),
    )
