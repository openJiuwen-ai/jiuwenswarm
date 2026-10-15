# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for the in-repo ``connectors/baidu-netdisk`` package source.

The directory is the package source for the Baidu Netdisk Hub connector
(remote-mcp + token against the official SSE endpoint
``https://mcp-pan.baidu.com/sse``). These tests guard the package contract
against drift: a broken manifest / mcp.json / token-schema.json fails here
instead of surfacing as a broken zip at Hub-publish or connect time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jiuwenswarm.server.runtime.mcp.credential import extract_placeholders
from jiuwenswarm.server.runtime.mcp.package_manifest import load_mcp_package
from jiuwenswarm.server.runtime.mcp.registry import _normalize_transport

REPO_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_DIR = REPO_ROOT / "connectors" / "baidu-netdisk"


def _load_package():
    return load_mcp_package(PACKAGE_DIR)


def test_package_contract() -> None:
    pkg = _load_package()
    assert pkg.package_id == "baidu-netdisk"
    assert pkg.version == "1.0.0"
    assert pkg.integration_type == "remote-mcp"
    assert pkg.credentials_type == "token"
    assert pkg.server_name == "baidu-netdisk"
    assert pkg.icon_file is not None and pkg.icon_file.name == "icon.png"
    assert [d.name for d in pkg.skill_dirs] == ["baidu-netdisk-guide"]


def test_mcp_json_shape() -> None:
    pkg = _load_package()
    cfg = pkg.server_config
    assert cfg is not None
    # SSE 端点必须显式声明 type: sse —— 缺省会被推导为 streamable-http，
    # 而官方服务器对 POST initialize 回 405。
    assert _normalize_transport(cfg.get("type", ""), cfg) == "sse"
    assert cfg["url"] == "https://mcp-pan.baidu.com/sse"
    assert cfg["headers"] == {
        "Authorization": "Bearer ${BAIDU_NETDISK_ACCESS_TOKEN}"
    }
    # timeout 单位是秒（build_config_entry 直接写入 timeout_s），600 = 10 分钟。
    assert cfg["timeout"] == 600


def test_token_schema_matches_placeholders() -> None:
    pkg = _load_package()
    assert pkg.server_config is not None
    placeholders = extract_placeholders(pkg.server_config)
    assert placeholders == {"BAIDU_NETDISK_ACCESS_TOKEN"}

    schema = json.loads(
        (PACKAGE_DIR / "token-schema.json").read_text(encoding="utf-8")
    )
    required = {f["key"] for f in schema["fields"] if f.get("required")}
    assert required == placeholders
    # docUrl 指向官方文档页（授权链接入口随官方更新），而非固定授权直链。
    assert schema["docUrl"].startswith("https://pan.baidu.com/union/doc/mcp-server/")
    # 桌面端一键授权（Electron 抓文档页提取直链 + 导航截取）：oauthDocUrl 同样必须是
    # 官方文档页而非固定授权直链——个人体验密钥（client_id）会不定期变更，硬编码直链
    # 会失效（设计文档风险 #1）；oauthSuccessPrefix 供 main.cjs 校验截取目标。
    oauth_doc_url = schema["oauthDocUrl"]
    assert oauth_doc_url.startswith("https://pan.baidu.com/union/doc/mcp-server/")
    assert "authorize" not in oauth_doc_url
    assert "oauthUrl" not in schema
    assert schema["oauthSuccessPrefix"] == "https://openapi.baidu.com/oauth/2.0/login_success"


def test_bundled_skill_frontmatter() -> None:
    skill_md = (
        PACKAGE_DIR / "skills" / "baidu-netdisk-guide" / "SKILL.md"
    ).read_text(encoding="utf-8")
    match = re.search(r"^name:\s*(\S+)", skill_md, re.MULTILINE)
    assert match is not None and match.group(1) == "baidu-netdisk-guide"
    # 写操作确认是行为需求的验收点，指引中必须保留该章节。
    assert "预览 → 确认" in skill_md
