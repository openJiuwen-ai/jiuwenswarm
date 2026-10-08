#!/usr/bin/env python3
"""企业版 config.yaml 生成器
用法：python3 update_conf.py [config_dir]（缺省 = $JIUWENSWARM_CONFIG_DIR；config.yaml 文件名固定）
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from ruamel.yaml import YAML

logger = logging.getLogger("update_conf")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SRC_FILE = REPO_ROOT / "jiuwenswarm" / "resources" / "config.yaml"
DEFAULT_CONFIG_DIR = Path.home() / ".jiuwenswarm" / "config"


def config_dir() -> Path:
    """配置目录：CLI 参数（argv[1]）> ``JIUWENSWARM_CONFIG_DIR`` > 默认目录。"""
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser()
    env = os.getenv("JIUWENSWARM_CONFIG_DIR", "").strip()
    return Path(env).expanduser() if env else DEFAULT_CONFIG_DIR


def setpath(d, dotted_path: str, value) -> None:
    """等价 yq eval '.a.b.c = value'：中间键不存在则创建。"""
    keys = dotted_path.split(".")
    cur = d
    for key in keys[:-1]:
        node = cur.get(key)
        if not isinstance(node, dict):
            node = {}
            cur[key] = node
        cur = node
    cur[keys[-1]] = value


def inject_feishu_bots(data) -> None:
    """原 gen_gateway_config_file 的 FEISHU_BOTS 注入，改由 env 驱动。"""
    data["channels"]["feishu"] = {}
    bots = os.getenv("FEISHU_BOTS", "").strip()
    for line in bots.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            logger.warning(
                "FEISHU_BOTS 行格式非法（应为 bot_name:app_id:app_secret）: %r", line
            )
            continue
        bot_name, app_id, app_secret = parts
        data["channels"]["feishu"][bot_name] = {
            "app_id": app_id,
            "app_secret": app_secret,
            "encrypt_key": "",
            "verification_token": "",
            "allow_from": [],
            "enable_streaming": True,
            "chat_id": "",
            "enabled": True,
        }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[update_conf] %(levelname)s %(message)s")

    dest = config_dir() / "config.yaml"
    if not SRC_FILE.is_file():
        logger.error("source not found: %s", SRC_FILE)
        return 1

    parser = YAML()
    parser.preserve_quotes = True
    data = parser.load(SRC_FILE)

    # ---- 企业版域修改（与原 yq 版等价；宏改为 ${ENV:-default} 插值占位）----
    setpath(data, "gateway.agent_client.type", "jiuwen")
    setpath(data, "gateway.edition", "enterprise")
    setpath(data, "config.source", "enterprise")
    setpath(data, "gateway.session_map_scope", "${GATEWAY_SESSION_MAP_SCOPE:-per_chat_bot}")
    setpath(data, "react.max_iterations", "${AGENT_SERVER_REACT_MAX_ITER:-100}")
    setpath(data, "react.evolution.enabled", False)
    setpath(data, "sandbox.enabled", True)
    setpath(data, "sandbox.startup_mode", "external")
    setpath(data, "sandbox.idle_ttl_seconds", 600)
    setpath(data, "sandbox.idle_check_interval", 180)
    setpath(data, "sandbox.url", "${JIUWENBOX_URL:-http://127.0.0.1:8321}")
    setpath(data, "sandbox.type", "jiuwenbox")
    setpath(data, "channels.ssh.host_key_path", "${JIUWENSWARM_CONFIG_DIR:-}/ssh_host_key")

    inject_feishu_bots(data)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        parser.dump(data, f)

    logger.info("generated %s (from %s)", dest, SRC_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
