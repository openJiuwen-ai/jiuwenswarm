# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``file_guard.paths`` entry identity used by compose and by the overlays."""

from __future__ import annotations


def _scope(path: str, match: str | None = None) -> tuple[str, str]:
    from jiuwenswarm.agents.harness.common.rails.permissions import permission_compose

    item = {"path": path}
    if match is not None:
        item["match"] = match
    return permission_compose.path_rule_scope(item)


def test_path_rule_scope_expands_home(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permission_compose

    # Stand in for the resolver instead of patching the standard library: HOME
    # is only honoured by posixpath, so the test must not depend on the
    # platform's expanduser implementation.
    monkeypatch.setattr(
        permission_compose,
        "_expand_user",
        lambda path: "/home/tester" + path[1:] if path.startswith("~") else path,
    )
    assert _scope("~/data", "prefix") == ("/home/tester/data", "prefix")


def test_path_rule_scope_normalizes_windows_home_after_expanduser(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permission_compose

    # os.path.expanduser on Windows returns backslashes; leaving them in gives
    # "~" and the equivalent explicit path two different scopes, so the two
    # never merge and the later layer's grant stays hidden.
    monkeypatch.setattr(
        permission_compose,
        "_expand_user",
        lambda path: "C:\\Users\\x" + path[1:] if path.startswith("~") else path,
    )
    assert _scope("~", "prefix") == _scope("C:/Users/x", "prefix")
    assert _scope("~/docs", "prefix")[0] == "C:/Users/x/docs"
    assert "\\" not in _scope("~/docs", "prefix")[0]


def test_path_rule_scope_drops_trailing_separator_from_expanded_home(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permission_compose

    # HOME may end with a separator and expanduser keeps it; the Engine's own
    # compile drops it, so the scope has to as well or "~" and "/home/x" still
    # miss each other.
    monkeypatch.setattr(
        permission_compose,
        "_expand_user",
        lambda path: "/home/x/" if path == "~" else path,
    )
    assert _scope("~", "prefix") == _scope("/home/x", "prefix")


def test_path_rule_scope_collapses_match_and_trailing_slash() -> None:
    # The Engine collapses every non-glob match to prefix and strips trailing
    # slashes from prefix rules.
    assert _scope("/data/", "regex") == _scope("/data", "prefix")


def test_path_rule_scope_keeps_glob_verbatim() -> None:
    # Glob paths are matched literally by the Engine, so a trailing slash is
    # part of the rule.
    assert _scope("/data/**/", "glob") != _scope("/data/**", "glob")


def test_path_rule_scope_case_sensitivity_follows_the_platform(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permission_compose

    monkeypatch.setattr(permission_compose, "_CASE_INSENSITIVE", False)
    assert _scope("/data/A") != _scope("/data/a")
    monkeypatch.setattr(permission_compose, "_CASE_INSENSITIVE", True)
    # Prefix rules keep the platform's case-insensitivity: the Engine's prefix
    # match goes through normcase-aware contains_path on Windows.
    assert _scope("/data/A") == _scope("/data/a")
    # Glob rules do not: the Engine matches them with a case-sensitive regex
    # on every platform, so folding here would merge two reachable rules into
    # one and hide the second layer's axes.
    assert _scope("/data/**/A", "glob") != _scope("/data/**/a", "glob")


def test_file_guard_glob_trailing_slash_stays_a_separate_rule() -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
        compose_host_effective_permissions,
    )

    # The Engine matches glob paths literally, so "/data/**/" and "/data/**"
    # are different rules and must not be collapsed into one.
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {"paths": [{"path": "/data/**/", "match": "glob", "read": "allow"}]},
        },
        session_permissions={
            "file_guard": {"paths": [{"path": "/data/**", "match": "glob", "write": "allow"}]}
        },
    )
    rules = {
        item["path"]: item
        for item in effective["file_guard"]["paths"]
        if item.get("match") == "glob" and item["path"].startswith("/data/")
    }
    assert set(rules) == {"/data/**/", "/data/**"}
    assert rules["/data/**/"]["read"] == "allow"
    assert rules["/data/**"]["write"] == "allow"


def test_file_guard_keeps_case_distinct_globs_apart(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permission_compose
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
        compose_host_effective_permissions,
    )

    # The Engine's glob match is case-sensitive on every platform, so folding
    # these two scopes (as a Windows host does for prefix rules) would merge
    # them into one entry and silently drop the session grant.
    monkeypatch.setattr(permission_compose, "_CASE_INSENSITIVE", True)
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {"paths": [{"path": "/data/**/A", "match": "glob", "read": "allow"}]},
        },
        session_permissions={
            "file_guard": {"paths": [{"path": "/data/**/a", "match": "glob", "write": "allow"}]}
        },
    )
    rules = {
        item["path"]: item
        for item in effective["file_guard"]["paths"]
        if item.get("match") == "glob"
    }
    assert set(rules) == {"/data/**/A", "/data/**/a"}
    assert rules["/data/**/A"]["read"] == "allow"
    assert rules["/data/**/a"]["write"] == "allow"


def test_file_guard_merges_tilde_and_explicit_windows_path(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permission_compose
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
        compose_host_effective_permissions,
    )

    # Both spellings compile to the same Engine prefix on Windows, so they must
    # collapse into one entry rather than the first one masking the other.
    monkeypatch.setattr(
        permission_compose,
        "_expand_user",
        lambda path: "C:\\Users\\x" + path[1:] if path.startswith("~") else path,
    )
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {"paths": [{"path": "~", "match": "prefix", "read": "allow"}]},
        },
        session_permissions={
            "file_guard": {"paths": [{"path": "C:/Users/x", "match": "prefix", "write": "allow"}]}
        },
    )
    rules = [
        item
        for item in effective["file_guard"]["paths"]
        if item.get("path") in ("~", "C:/Users/x")
    ]
    assert len(rules) == 1
    assert rules[0]["read"] == "allow"
    assert rules[0]["write"] == "allow"
