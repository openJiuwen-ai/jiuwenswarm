# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""规则判定接口（api.py）契约测试：三态 + none、ask 静态化、目标归一、导出快照。"""
from __future__ import annotations

import pytest

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.agents.harness.common.rails.security_lists import (
    composer as composer_mod,
    normalize,
)
from jiuwenswarm.agents.harness.common.rails.security_lists.api import (
    check_command,
    check_domain,
    check_domain_static,
    check_path,
    check_path_static,
    export_domain_rules,
    export_path_rules,
)


def _section(user=None, defaults=None):
    return {
        "security_lists": {
            "version": 3,
            "user": user or [],
            "cloud": {"sync_version": "", "synced_at": "", "records": []},
            "defaults": defaults or {},
        }
    }


@pytest.fixture
def rules(tmp_path, monkeypatch):
    """把 config.yaml 指向临时文件；builtin 投影置空（只测 user/defaults 两源）。"""
    import yaml

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: path)
    # composer 是模块级 import，patch 要打在它自己的命名空间上
    monkeypatch.setattr(normalize, "project_builtin", lambda: [])
    monkeypatch.setattr(composer_mod, "project_builtin", lambda: [])

    def write(section):
        path.write_text(yaml.safe_dump(section, allow_unicode=True), encoding="utf-8")

    return write


def rec(pattern, action, *, list_type="domain", match="exact", rid="r1", mode="*"):
    return {
        "id": rid, "type": list_type, "pattern": pattern, "match": match,
        "enabled": True, "cells": {mode: {"*": action} if list_type != "file_path" else {"read": action}},
        "source": "user",
    }


# ---------------------------------------------------------------------------
# check_domain
# ---------------------------------------------------------------------------


def test_check_domain_deny_from_record(rules):
    rules(_section(user=[rec("evil.example", "deny")]))
    verdict = check_domain("evil.example", mode="default")
    assert verdict.action == "deny"
    assert verdict.list_type == "domain" and verdict.source == "user"
    assert verdict.pattern == "evil.example"
    assert "用户名单" in verdict.reason


def test_check_domain_none_without_opinion(rules):
    rules(_section())
    verdict = check_domain("unlisted.example", mode="default")
    assert verdict.action == "none"
    assert verdict.record_id == "" and verdict.pattern == ""


def test_check_domain_normalizes_url_host_and_case(rules):
    rules(_section(user=[rec("evil.example", "deny")]))
    for target in ("HTTPS://Evil.Example/path?q=1", "evil.example.", "https://a.evil.example/x"):
        assert check_domain(target, mode="default").action == "deny", target


def test_check_domain_honours_defaults_whitelist(rules):
    """未列出即拒（白名单）由 defaults 表达；列出的 allow 记录不被兜底抹掉。"""
    rules(_section(
        user=[rec("allowed.example", "allow")],
        defaults={"*": {"domain": "deny"}},
    ))
    assert check_domain("allowed.example", mode="default").action == "allow"
    unlisted = check_domain("other.example", mode="default")
    assert unlisted.action == "deny" and unlisted.source == "default"


def test_check_domain_static_degrades_ask_to_deny_and_none_to_allow(rules):
    """静态化的降级规则：``ask → deny``（fail-closed）；``none`` 无表态 → ``allow``。

    ``none`` 不能在静态层转 deny —— 那等于"没配规则就拦一切"。真要白名单，
    由 ``defaults`` 给出 ``deny``（此时裁决本身就不是 ``none`` 了）。
    """
    rules(_section(user=[rec("askme.example", "ask")]))
    assert check_domain_static("askme.example", mode="default") == "deny"
    assert check_domain_static("unlisted.example", mode="default") == "allow"

    rules(_section(user=[rec("ok.example", "allow")]))
    assert check_domain_static("ok.example", mode="default") == "allow"


def test_check_domain_static_denies_on_corrupt_config(rules):
    """异常/名单损坏 → deny（fail-closed，与 rail 同款）。"""
    rules({"security_lists": "oops"})
    assert check_domain_static("any.example", mode="default") == "deny"


# ---------------------------------------------------------------------------
# check_path / check_command
# ---------------------------------------------------------------------------


def test_check_path_resolves_per_axis(rules):
    rules(_section(user=[rec("C:/data", "deny", list_type="file_path", match="prefix")]))
    assert check_path("C:/data/sub/f.txt", op="read", mode="default").action == "deny"
    # 该记录只写了 read 轴 → write 无表态
    assert check_path("C:/data/sub/f.txt", op="write", mode="default").action == "none"


def test_check_path_static_degrades_ask_to_deny_and_none_to_allow(rules):
    rules(_section(user=[
        rec("C:/data", "ask", list_type="file_path", match="prefix"),
    ]))
    assert check_path_static("C:/data/x.txt", op="read", mode="default") == "deny"
    assert check_path_static("C:/other/x.txt", op="read", mode="default") == "allow"


def test_check_command_checks_exe_and_full_line(rules):
    rules(_section(user=[
        rec("curl", "deny", list_type="command", match="exact", rid="exe_rule"),
        rec("git push *", "ask", list_type="command", match="glob", rid="line_rule"),
    ]))
    # exact 规则命中 exe 名
    assert check_command("curl", "curl http://x", mode="default").action == "deny"
    # glob 规则命中整行
    assert check_command("git", "git push origin main", mode="default").action == "ask"
    # 都不命中 → none
    assert check_command("ls", "ls -la", mode="default").action == "none"


# ---------------------------------------------------------------------------
# export_*
# ---------------------------------------------------------------------------


def test_export_domain_rules_lists_defaults_and_stamp(rules):
    rules(_section(
        user=[
            rec("allowed.example", "allow", rid="a"),
            rec("bad.example", "deny", rid="b"),
            rec("ask.example", "ask", rid="c"),
        ],
        defaults={"*": {"domain": "deny"}},
    ))
    export = export_domain_rules(mode="default")
    assert sorted(export.allow) == ["allowed.example"]
    # ask 在静态执行面无交互可问 → fail-closed 归入 deny
    assert sorted(export.deny) == ["ask.example", "bad.example"]
    assert export.defaults == "deny"
    assert export.stamp

    # stamp 随内容变化（供执行面判断是否需要重渲染）
    rules(_section(user=[rec("allowed.example", "allow", rid="a")]))
    assert export_domain_rules(mode="default").stamp != export.stamp


def test_export_domain_rules_defaults_none_when_unset(rules):
    rules(_section(user=[rec("bad.example", "deny")]))
    export = export_domain_rules(mode="default")
    assert export.defaults is None


def test_export_path_rules_axes(rules):
    rules(_section(user=[
        {"id": "p1", "type": "file_path", "pattern": "C:/data", "match": "prefix",
         "enabled": True, "cells": {"*": {"read": "allow", "write": "deny"}},
         "source": "user"},
    ]))
    export = export_path_rules(mode="default")
    assert export.allow_read == ["C:/data"]
    assert export.deny_write == ["C:/data"]
    assert export.deny_read == []
    assert export.stamp
