# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for the in-repo ``connectors/qq-mail`` package source.

The directory is the package source for the QQ Mail Hub connector — a
stdio-mcp package driving ``@codefuturist/email-mcp`` over the standard
IMAP/SMTP protocol (``imap.qq.com`` / ``smtp.qq.com``) with the user's
email address + IMAP/SMTP authorization code as credentials. These tests
guard the package contract against drift so a broken manifest / mcp.json /
token-schema.json fails here instead of surfacing as a broken zip at
Hub-publish or connect time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jiuwenswarm.server.runtime.mcp.credential import extract_placeholders
from jiuwenswarm.server.runtime.mcp.package_manifest import load_mcp_package
from jiuwenswarm.server.runtime.mcp.registry import _normalize_transport

REPO_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_DIR = REPO_ROOT / "connectors" / "qq-mail"


def _load_package():
    return load_mcp_package(PACKAGE_DIR)


def test_package_contract() -> None:
    pkg = _load_package()
    assert pkg.package_id == "qq-mail"
    assert pkg.version == "1.0.0"
    assert pkg.integration_type == "stdio-mcp"
    assert pkg.credentials_type == "token"
    assert pkg.server_name == "qq-mail"
    assert pkg.icon_file is not None and pkg.icon_file.name == "icon.png"
    assert [d.name for d in pkg.skill_dirs] == ["qq-mail-guide"]


def test_mcp_json_shape() -> None:
    pkg = _load_package()
    cfg = pkg.server_config
    assert cfg is not None
    # stdio 形态：本地 npx 拉起 email-mcp，预检走平台现成 stdio 路径。
    assert _normalize_transport(cfg.get("type", ""), cfg) == "stdio"
    assert cfg["command"] == "npx"
    args = cfg["args"]
    # 供应链安全：npm 包版本必须锁定（不得使用无版本的 latest 标签）。
    assert any(
        re.fullmatch(r"@codefuturist/email-mcp@\d+\.\d+\.\d+", a) for a in args
    )
    assert args[-1] == "stdio"
    # QQ 邮箱 IMAP/SMTP 主机写死（993/465 TLS 为 email-mcp 默认值，无需显式端口）。
    assert cfg["env"]["MCP_EMAIL_IMAP_HOST"] == "imap.qq.com"
    assert cfg["env"]["MCP_EMAIL_SMTP_HOST"] == "smtp.qq.com"
    # timeout 单位是秒（build_config_entry 直接写入 timeout_s），600 = 10 分钟。
    assert cfg["timeout"] == 600


def test_token_schema_matches_placeholders() -> None:
    pkg = _load_package()
    assert pkg.server_config is not None
    placeholders = extract_placeholders(pkg.server_config)
    assert placeholders == {"QQ_MAIL_ADDRESS", "QQ_MAIL_AUTH_CODE"}

    schema = json.loads(
        (PACKAGE_DIR / "token-schema.json").read_text(encoding="utf-8")
    )
    required = {f["key"] for f in schema["fields"] if f.get("required")}
    assert required == placeholders
    # 授权码字段必须口令化展示；邮箱地址为明文输入框。
    fields = {f["key"]: f for f in schema["fields"]}
    assert fields["QQ_MAIL_AUTH_CODE"]["type"] == "password"
    assert fields["QQ_MAIL_ADDRESS"]["type"] == "text"
    # docUrl 指向 QQ 邮箱官方帮助（IMAP/SMTP 服务开启与授权码获取）。
    assert schema["docUrl"].startswith("https://service.mail.qq.com/")


def test_bundled_skill_frontmatter() -> None:
    skill_md = (
        PACKAGE_DIR / "skills" / "qq-mail-guide" / "SKILL.md"
    ).read_text(encoding="utf-8")
    match = re.search(r"^name:\s*(\S+)", skill_md, re.MULTILINE)
    assert match is not None and match.group(1) == "qq-mail-guide"
    # IMAP/SMTP 通道无服务端两阶段确认，写操作确认纪律、授权码指引、
    # 惰性连接自检（check_health）与邮件内容注入防护是行为需求的验收点，
    # 指引中必须保留对应章节。
    assert "写操作确认纪律" in skill_md
    assert "授权码" in skill_md
    assert "check_health" in skill_md
    assert "提示词注入" in skill_md
