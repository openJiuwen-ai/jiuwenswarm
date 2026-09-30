# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists.matcher 单测：路径规范化、glob、域名边界、命令匹配、extract_targets。"""
from __future__ import annotations

import sys

import pytest

from jiuwenswarm.agents.harness.common.rails.security_lists.matcher import (
    extract_targets,
    match_record,
)
from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
    SecurityListRecord,
)


def rec(**kw) -> SecurityListRecord:
    base = dict(id="t", type="file_path", pattern="", match="prefix",
                cells={"*": {"*": "allow"}})
    base.update(kw)
    return SecurityListRecord(**base)


# ---------------------------------------------------------------------------
# file_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern,target,expected", [
    (r"C:\Data", "c:/data/sub/file.txt", True),     # 大小写 + 反斜杠归一（Windows）
    ("C:/data", "C:/data", True),                   # 等值命中
    ("C:/data/", "C:/data/sub", True),              # 尾斜杠容忍
    ("C:/data", "C:/database/x", False),            # 前缀边界：不能串到兄弟目录
    ("C:/data", "D:/data", False),                  # 跨盘符
    ("~/docs", "~/docs/a.txt", True),               # ~ 展开
])
def test_file_prefix(pattern, target, expected):
    if sys.platform != "win32" and pattern.startswith(("C:", "D:")):
        pytest.skip("Windows 路径语义")
    assert match_record(rec(pattern=pattern, match="prefix"), target) is expected


@pytest.mark.parametrize("pattern,target,expected", [
    ("**/.env", "C:/a/b/.env", True),
    ("**/*.pem", "C:/x/key.pem", True),
    ("C:/data/**", "C:/data/a/b.txt", True),
    ("C:/data/**", "D:/data/a.txt", False),
    (r"C:\data\**\*.tmp", "c:/data/x/y.tmp", True),  # 反斜杠 + 大小写
    ("**/.ssh/**", "C:/Users/u/.ssh/id_rsa", True),
])
def test_file_glob(pattern, target, expected):
    if sys.platform != "win32" and ("C:" in pattern or "D:" in pattern):
        pytest.skip("Windows 路径语义")
    assert match_record(rec(pattern=pattern, match="glob"), target) is expected


# ---------------------------------------------------------------------------
# domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern,match,host,expected", [
    ("example.com", "exact", "example.com", True),       # 裸域
    ("example.com", "exact", "a.example.com", True),     # 子域
    ("example.com", "exact", "A.Example.COM", True),     # 大小写
    ("example.com", "exact", "notexample.com", False),   # 后缀串但非子域
    ("example.com", "exact", "example.com.evil.com", False),
    ("*.example.com", "wildcard", "a.example.com", True),
    ("*.example.com", "wildcard", "a.b.example.com", True),
    ("*.example.com", "wildcard", "example.com", False),  # wildcard 仅子域
])
def test_domain(pattern, match, host, expected):
    assert match_record(rec(type="domain", pattern=pattern, match=match), host) is expected


@pytest.mark.parametrize("pattern,match,host", [
    ("evil.example", "exact", "evil.example."),       # DNS 尾点等价：不能绕过 deny
    ("*.evil.example", "wildcard", "a.evil.example."),
    ("evil.example.", "exact", "evil.example"),       # pattern 侧带尾点同样归一
    ("evil.example.", "exact", "evil.example."),
])
def test_domain_trailing_dot_is_normalized(pattern, match, host):
    """尾点与裸域在 DNS 上等价，必须归一——否则加个 '.' 就能绕过 deny 规则（设计 §7）。"""
    assert match_record(rec(type="domain", pattern=pattern, match=match), host) is True


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern,match,target,expected", [
    ("git", "exact", "git", True),
    ("git", "exact", "git.exe", True),
    ("git", "exact", "GIT", True),
    ("git", "exact", "gitx", False),
    ("git *", "glob", "git status", True),
    ("git *", "glob", "git", True),           # 尾 " *" 可匹配无参数
    ("git *", "glob", "gitx status", False),
    (r"(?i)(^|[\s;&|()])rm\b", "regex", "rm -rf /tmp", True),
    (r"(?i)(^|[\s;&|()])rm\b", "regex", "git rm a", True),   # search 语义
    (r"(?i)(^|[\s;&|()])rm\b", "regex", "rmdir x", False),
])
def test_command(pattern, match, target, expected):
    assert match_record(rec(type="command", pattern=pattern, match=match), target) is expected


# ---------------------------------------------------------------------------
# extract_targets
# ---------------------------------------------------------------------------


def test_extract_read_file(tmp_path):
    targets = extract_targets("read_file", {"file_path": "a/b.txt"}, workspace=tmp_path)
    assert len(targets) == 1
    list_type, target, op = targets[0]
    assert (list_type, op) == ("file_path", "read")
    assert target.replace("\\", "/").endswith("a/b.txt")


def test_extract_write_file(tmp_path):
    targets = extract_targets("write_file", {"file_path": "out.txt"}, workspace=tmp_path)
    assert [op for _, _, op in targets] == ["write"]


def test_extract_shell_command_segments_and_paths(tmp_path):
    targets = extract_targets(
        "bash", {"command": "cat ./secret.txt && rm -rf ./junk"}, workspace=tmp_path,
    )
    commands = [t for ty, t, _op in targets if ty == "command"]
    assert any("cat ./secret.txt && rm -rf ./junk" == c for c in commands)  # 整行
    assert "cat" in commands and "rm" in commands                            # exe 名
    paths = [(t, op) for ty, t, op in targets if ty == "file_path"]
    assert any(p.replace("\\", "/").endswith("secret.txt") and op == "read" for p, op in paths)
    assert any(p.replace("\\", "/").endswith("junk") and op == "write" for p, op in paths)


def test_extract_shell_redirect_write(tmp_path):
    targets = extract_targets("bash", {"command": "echo hi > out.log"}, workspace=tmp_path)
    assert any(
        ty == "file_path" and t.replace("\\", "/").endswith("out.log") and op == "write"
        for ty, t, op in targets
    )


def test_extract_fetch_domain():
    targets = extract_targets("fetch_webpage", {"url": "https://Sub.Example.com:8443/x?y=1"})
    assert ("domain", "sub.example.com", "*") in targets


def test_extract_non_fetch_tool_ignores_url():
    targets = extract_targets("some_mcp_tool", {"url": "http://localhost:8080/api"})
    assert [t for t in targets if t[0] == "domain"] == []


def test_extract_empty_args():
    assert extract_targets("bash", {}) == []
    assert extract_targets("bash", None) == []
