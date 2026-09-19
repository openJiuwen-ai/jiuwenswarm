# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""extra.paths contract, write-root filter, auth, and implicit-ACE regressions."""

from __future__ import annotations

import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from jiuwenbox.server.access import (
    AccessDeniedError,
    AccessExtraValidationError,
    canonicalize_path,
    parse_extra_paths,
    path_in_roots,
    resolve_access_context,
)
from jiuwenbox.server.auth import BearerTokenAuthMiddleware
from jiuwenbox.supervisor import win_acl


class TestParseExtraPaths:
    def test_missing_required_denied_when_enforced(self, monkeypatch):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: True)
        with pytest.raises(AccessDeniedError):
            parse_extra_paths(None, required=True)
        with pytest.raises(AccessDeniedError):
            parse_extra_paths({"paths": []}, required=True)

    def test_missing_optional_ok_when_not_enforced(self, monkeypatch):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: False)
        assert parse_extra_paths(None, required=True) == []
        assert parse_extra_paths({"paths": []}, required=True) == []

    def test_malformed_json_is_422(self):
        with pytest.raises(AccessExtraValidationError):
            parse_extra_paths("{not-json", required=False)
        with pytest.raises(AccessExtraValidationError):
            parse_extra_paths({"nope": []}, required=False)
        with pytest.raises(AccessExtraValidationError):
            parse_extra_paths({"paths": [1]}, required=False)

    def test_over_limit_is_422(self):
        with pytest.raises(AccessExtraValidationError):
            parse_extra_paths({"paths": [f"/p{i}" for i in range(65)]}, required=False)

    def test_json_string_and_model(self):
        assert parse_extra_paths('{"paths": ["C:\\\\ws", ""]}', required=False) == ["C:\\ws"]


class TestPathContainment:
    def test_equal_root_and_child_ok(self, tmp_path):
        root = tmp_path / "foo"
        child = root / "nested" / "a.txt"
        child.parent.mkdir(parents=True)
        child.write_text("x", encoding="utf-8")
        assert path_in_roots(str(root), [str(root)])
        assert path_in_roots(str(child), [str(root)])

    def test_prefix_sibling_rejected(self, tmp_path):
        foo = tmp_path / "foo"
        foo2 = tmp_path / "foo2"
        foo.mkdir()
        foo2.mkdir()
        assert not path_in_roots(str(foo2), [str(foo)])

    def test_dotdot_escape_rejected(self, tmp_path):
        foo = tmp_path / "foo"
        outside = tmp_path / "other"
        foo.mkdir()
        outside.mkdir()
        sneaky = foo / ".." / "other" / "x.txt"
        sneaky.parent.mkdir(parents=True, exist_ok=True)
        sneaky.write_text("x", encoding="utf-8")
        assert not path_in_roots(str(sneaky), [str(foo)])

    def test_unc_and_device_rejected(self):
        with pytest.raises(AccessDeniedError):
            canonicalize_path(r"\\server\share")
        with pytest.raises(AccessDeniedError):
            canonicalize_path("//server/share")
        with pytest.raises(AccessDeniedError):
            canonicalize_path(r"\\.\NUL")

    @pytest.mark.skipif(sys.platform != "win32", reason="POSIX /tmp mapping is a Windows abspath trap")
    def test_posix_tmp_is_not_mapped_to_c_tmp(self):
        with pytest.raises(AccessDeniedError, match="POSIX path"):
            canonicalize_path("/tmp")
        with pytest.raises(AccessDeniedError, match="POSIX path"):
            canonicalize_path("/home/foo")
        # Drive-qualified still ok (explicit Windows path, not the /tmp trap).
        canonicalize_path(r"C:\Users")


class TestPosixExtraDroppedOnWindows:
    def test_resolve_drops_slash_tmp_keeps_windows_root(self, monkeypatch, tmp_path):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: True)
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "a.txt"
        target.write_text("x", encoding="utf-8")
        ctx = resolve_access_context(
            sandbox_id="sb",
            extra={"paths": ["/tmp", str(ws)]},
            actual_paths=[str(target)],
            op="write",
            required=True,
            grant_write=False,
        )
        keys = {os.path.normcase(os.path.abspath(p)) for p in ctx.extra_roots}
        assert os.path.normcase(str(ws.resolve())) in keys
        assert os.path.normcase(os.path.abspath("/tmp")) not in keys
        assert os.path.normcase(r"C:\tmp") not in keys


class TestResolveAccessContextLinux:
    def test_linux_does_not_enforce_missing_extra(self, monkeypatch, tmp_path):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: False)
        target = tmp_path / "a.txt"
        target.write_text("x", encoding="utf-8")
        ctx = resolve_access_context(
            sandbox_id="sb",
            extra=None,
            actual_paths=[str(target)],
            op="read",
            required=True,
            grant_write=False,
        )
        assert ctx.windows is False
        assert ctx.write_cap_sids == ()


class TestWorkspacePolicyAccess:
    def test_empty_extra_ok_inside_workspace(self, monkeypatch, tmp_path):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: True)
        ws = tmp_path / "ws"
        ws.mkdir()
        ctx = resolve_access_context(
            sandbox_id="sb",
            extra=None,
            actual_paths=[str(ws / "a.txt")],
            op="read",
            required=True,
            grant_write=False,
            workspace_roots=[str(ws)],
        )
        keys = {os.path.normcase(os.path.abspath(p)) for p in ctx.extra_roots}
        assert os.path.normcase(str(ws.resolve())) in keys

    def test_empty_extra_without_workspace_denied(self, monkeypatch):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: True)
        with pytest.raises(AccessDeniedError, match="extra.paths"):
            resolve_access_context(
                sandbox_id="sb",
                extra=None,
                actual_paths=[],
                op="exec",
                required=True,
                grant_write=False,
            )

    def test_outside_workspace_denied(self, monkeypatch, tmp_path):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: True)
        ws = tmp_path / "ws"
        other = tmp_path / "other"
        ws.mkdir()
        other.mkdir()
        with pytest.raises(AccessDeniedError, match="outside extra.paths"):
            resolve_access_context(
                sandbox_id="sb",
                extra=None,
                actual_paths=[str(other)],
                op="exec",
                required=True,
                grant_write=False,
                workspace_roots=[str(ws)],
            )

    def test_grant_write_unions_workspace_and_extra_sids(self, monkeypatch, tmp_path):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: True)
        ws = tmp_path / "ws"
        extra_dir = tmp_path / "extra"
        ws.mkdir()
        extra_dir.mkdir()
        monkeypatch.setattr(
            win_acl, "assert_write_roots",
            lambda paths, deny_write=None: list(paths),
        )
        monkeypatch.setattr(
            win_acl, "cap_sids_for_write_roots",
            lambda paths: [f"SID-{i}" for i, _p in enumerate(paths)],
        )
        ctx = resolve_access_context(
            sandbox_id="sb",
            extra={"paths": [str(extra_dir)]},
            actual_paths=[str(ws)],
            op="exec",
            required=True,
            grant_write=True,
            workspace_roots=[str(ws)],
        )
        assert ctx.write_cap_sids == ("SID-0", "SID-1")
        keys = {os.path.normcase(os.path.abspath(p)) for p in ctx.write_roots}
        assert os.path.normcase(str(ws.resolve())) in keys
        assert os.path.normcase(str(extra_dir.resolve())) in keys

    def test_unrequired_empty_extra_still_injects_workspace_sids(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr("jiuwenbox.server.access.extra_enforced", lambda: True)
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            win_acl, "assert_write_roots",
            lambda paths, deny_write=None: list(paths),
        )
        monkeypatch.setattr(
            win_acl, "cap_sids_for_write_roots",
            lambda paths: ["WS-SID"],
        )
        ctx = resolve_access_context(
            sandbox_id="sb",
            extra=None,
            actual_paths=[],
            op="exec",
            required=False,
            grant_write=True,
            workspace_roots=[str(ws)],
        )
        assert ctx.write_cap_sids == ("WS-SID",)
        assert ctx.extra_roots == ()


class TestWriteRootFilter:
    def test_volume_root_dropped(self):
        if sys.platform == "win32":
            dropped = win_acl.filter_write_roots([r"C:\\", "C:"])
            assert dropped == []
        else:
            dropped = win_acl.filter_write_roots(["/"])
            assert dropped == []

    def test_userprofile_kept_as_single_root(self, tmp_path, monkeypatch):
        """USERPROFILE 作为整根 allow_write, 不再展开成一级子目录."""
        profile = tmp_path / "profile"
        docs = profile / "Documents"
        desktop = profile / "Desktop"
        appdata = profile / "AppData"
        hidden = profile / ".jiuwenswarm"
        docs.mkdir(parents=True)
        desktop.mkdir()
        appdata.mkdir()
        hidden.mkdir()
        monkeypatch.setattr(
            win_acl, "_user_profile_stop_keys",
            lambda: {os.path.normcase(str(profile.resolve()))},
        )
        monkeypatch.setattr(win_acl, "_sensitive_write_roots", lambda: [])
        out = win_acl.filter_write_roots([str(profile)])
        keys = {os.path.normcase(os.path.abspath(p)) for p in out}
        assert os.path.normcase(str(profile.resolve())) in keys
        assert os.path.normcase(str(docs.resolve())) not in keys
        assert os.path.normcase(str(desktop.resolve())) not in keys
        assert os.path.normcase(str(appdata.resolve())) not in keys
        assert os.path.normcase(str(hidden.resolve())) not in keys

    def test_schedule_tree_propagate_dedup(self, tmp_path, monkeypatch):
        target = tmp_path / "big"
        target.mkdir()
        put: list[str] = []

        class _FakeQueue:
            def put(self, path: str) -> None:
                put.append(path)

        monkeypatch.setattr(win_acl, "_ensure_propagate_worker", lambda: None)
        monkeypatch.setattr(win_acl, "_propagate_jobs", _FakeQueue())
        with win_acl._propagate_lock:  # noqa: SLF001
            win_acl._propagate_queued.clear()  # noqa: SLF001
            win_acl._propagate_done.clear()  # noqa: SLF001
        win_acl.schedule_tree_propagate(str(target))
        win_acl.schedule_tree_propagate(str(target))
        assert put == [str(target)]
        with win_acl._propagate_lock:  # noqa: SLF001
            key = win_acl._propagate_path_key(str(target))  # noqa: SLF001
            win_acl._propagate_queued.discard(key)  # noqa: SLF001
            win_acl._propagate_done.add(key)  # noqa: SLF001
        win_acl.schedule_tree_propagate(str(target))
        assert put == [str(target)]
        win_acl.schedule_tree_propagate(str(target), force=True)
        assert put == [str(target), str(target)]

    def test_sensitive_and_deny_write_dropped(self, tmp_path, monkeypatch):
        home = tmp_path / "jiuwenbox-home"
        allowed = tmp_path / "ws"
        denied = tmp_path / "ws" / "secret"
        home.mkdir()
        denied.mkdir(parents=True)
        monkeypatch.setattr(win_acl, "_sensitive_write_roots", lambda: [str(home)])
        monkeypatch.setattr(win_acl, "_user_profile_stop_keys", lambda: set())
        out = win_acl.filter_write_roots(
            [str(home), str(allowed), str(denied)],
            deny_write=[str(denied)],
        )
        keys = {os.path.normcase(os.path.abspath(p)) for p in out}
        assert os.path.normcase(str(home.resolve())) not in keys
        assert os.path.normcase(str(denied.resolve())) not in keys
        assert os.path.normcase(str(allowed.resolve())) in keys

    def test_assert_write_roots_fail_closed(self, monkeypatch):
        monkeypatch.setattr(win_acl, "filter_write_roots", lambda paths, **kw: [])
        with pytest.raises(win_acl.WriteRootDenied):
            win_acl.assert_write_roots([r"C:\\"], deny_write=[])

    def test_sandbox_workspace_survives_sensitive_filter(self, monkeypatch):
        """workspace 在 JIUWENBOX_HOME 之下, 必须被豁免, 否则沙箱写不了自己的 cwd."""
        home = os.path.normcase(os.path.join("C:", os.sep, "home", "jiuwenbox"))
        ws = os.path.join(home, "workspace")
        monkeypatch.setattr(win_acl, "_sensitive_write_roots", lambda: [home])
        monkeypatch.setattr(win_acl, "_sensitive_write_exempt_roots", lambda: [ws])
        monkeypatch.setattr(win_acl, "_path_key", lambda p: os.path.normcase(str(p)))
        monkeypatch.setattr(win_acl, "_user_profile_stop_keys", lambda: set())
        monkeypatch.setattr(win_acl, "_is_heavy_directory", lambda p: False)

        sandbox_ws = os.path.join(ws, "sb-1")
        out = win_acl.filter_write_roots([sandbox_ws, os.path.join(home, "secrets")])
        assert [os.path.normcase(p) for p in out] == [os.path.normcase(sandbox_ws)]

    def test_ancestor_of_sensitive_root_is_kept(self, monkeypatch):
        """敏感根的祖先不该被丢: 计划语义是整根 allow_write + deny_write 雕空."""
        desktop = os.path.normcase(os.path.join("C:", os.sep, "users", "u", "desktop"))
        creds = os.path.join(desktop, ".credentials")
        monkeypatch.setattr(win_acl, "_sensitive_write_roots", lambda: [creds])
        monkeypatch.setattr(win_acl, "_sensitive_write_exempt_roots", lambda: [])
        monkeypatch.setattr(win_acl, "_path_key", lambda p: os.path.normcase(str(p)))
        monkeypatch.setattr(win_acl, "_user_profile_stop_keys", lambda: set())
        monkeypatch.setattr(win_acl, "_is_heavy_directory", lambda p: False)

        out = win_acl.filter_write_roots([desktop, creds])
        assert [os.path.normcase(p) for p in out] == [desktop]

    def test_reconcile_does_not_apply_admission_limits(self):
        """max_roots 在 reconcile 里截断会把已授权根当成 stale 撤销."""
        src = inspect.getsource(win_acl.reconcile_live_write_roots)
        assert "drop_heavy=False" in src
        assert "max_roots=None" in src

    def test_filter_keeps_heavy_directory(self, tmp_path, monkeypatch):
        heavy = tmp_path / "huge"
        heavy.mkdir()
        monkeypatch.setattr(win_acl, "_is_heavy_directory", lambda p: True)
        monkeypatch.setattr(win_acl, "_user_profile_stop_keys", lambda: set())
        monkeypatch.setattr(win_acl, "_sensitive_write_roots", lambda: [])
        out = win_acl.filter_write_roots([str(heavy)], drop_heavy=True)
        keys = {os.path.normcase(os.path.abspath(p)) for p in out}
        assert os.path.normcase(str(heavy.resolve())) in keys
        src = inspect.getsource(win_acl.filter_write_roots)
        assert "drop heavy directory" not in src


class TestAclSplitPropagate:
    def test_small_dir_enqueues_root_once(self, tmp_path, monkeypatch):
        root = tmp_path / "small"
        root.mkdir()
        put: list[str] = []

        class _FakeQueue:
            def put(self, path: str) -> None:
                put.append(path)

        monkeypatch.setattr(win_acl, "_ensure_propagate_worker", lambda: None)
        monkeypatch.setattr(win_acl, "_propagate_jobs", _FakeQueue())
        with win_acl._propagate_cv:  # noqa: SLF001
            win_acl._propagate_queued.clear()  # noqa: SLF001
            win_acl._propagate_done.clear()  # noqa: SLF001
        win_acl.schedule_tree_propagate(str(root))
        assert put == [str(root)]

    def test_split_enqueues_children_not_parent_tree(self, tmp_path, monkeypatch):
        root = tmp_path / "big"
        child_a = root / "a"
        child_b = root / "b"
        child_a.mkdir(parents=True)
        child_b.mkdir()
        (root / "f.txt").write_text("x", encoding="utf-8")

        class _Sd:
            def GetSecurityDescriptorDacl(self):
                return "acl"

        win32 = MagicMock()
        win32.GetFileSecurity.return_value = _Sd()
        monkeypatch.setattr(win_acl, "_ensure_pywin32", lambda: (win32, None, None))
        monkeypatch.setattr(
            win_acl, "_collect_explicit_inheritable_aces",
            lambda dacl: [(1, 3, 0x1F, "sid")],
        )
        pushed: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            win_acl, "_push_inheritable_aces_to_child",
            lambda child, aces, directory=True: pushed.append((child, directory)),
        )
        queued: list[tuple[str, int]] = []
        monkeypatch.setattr(
            win_acl, "schedule_tree_propagate",
            lambda path, force=False, depth=0: queued.append((path, depth)),
        )
        parent_prop: list[str] = []
        monkeypatch.setattr(
            win_acl, "_write_file_dacl_propagate",
            lambda path, acl, sd=None: parent_prop.append(path),
        )
        win_acl._split_and_enqueue_children(str(root), depth=0)  # noqa: SLF001
        queued_keys = {os.path.normcase(p) for p, _d in queued}
        assert os.path.normcase(str(child_a)) in queued_keys
        assert os.path.normcase(str(child_b)) in queued_keys
        assert os.path.normcase(str(root)) not in queued_keys
        assert queued and all(d == 1 for _p, d in queued)
        assert parent_prop == []
        file_pushed = [p for p, is_dir in pushed if not is_dir]
        assert any(os.path.normcase(p).endswith("f.txt") for p in file_pushed)

    def test_run_tree_propagate_split_skips_whole_tree(self, tmp_path, monkeypatch):
        root = tmp_path / "big"
        root.mkdir()
        monkeypatch.setattr(win_acl, "_dir_split_plan", lambda path, depth=0: "split")
        split_calls: list[tuple[str, int]] = []
        monkeypatch.setattr(
            win_acl, "_split_and_enqueue_children",
            lambda path, depth=0: split_calls.append((path, depth)),
        )
        prop_calls: list[str] = []
        monkeypatch.setattr(
            win_acl, "_write_file_dacl_propagate",
            lambda path, acl, sd=None: prop_calls.append(path),
        )
        win_acl._run_tree_propagate(str(root), depth=0)  # noqa: SLF001
        assert split_calls == [(str(root), 0)]
        assert prop_calls == []

    def test_max_depth_stops_splitting(self, tmp_path):
        root = tmp_path / "leaf"
        root.mkdir()
        win_acl._split_plan_cache.clear()  # noqa: SLF001
        plan = win_acl._dir_split_plan(  # noqa: SLF001
            str(root), depth=win_acl.ACL_SPLIT_MAX_DEPTH,
        )
        assert plan == "propagate"

    def test_many_direct_dirs_split(self, tmp_path, monkeypatch):
        root = tmp_path / "wide"
        root.mkdir()
        win_acl._split_plan_cache.clear()  # noqa: SLF001
        monkeypatch.setattr(
            win_acl, "_probe_dir_weight",
            lambda p: (0, win_acl.ACL_SPLIT_DIRECT_DIRS, 32, True),
        )
        assert win_acl._dir_split_plan(str(root), depth=0) == "split"  # noqa: SLF001

    def test_propagate_pool_worker_cap(self, monkeypatch):
        monkeypatch.setattr(win_acl, "_propagate_dispatcher_main", lambda: None)
        win_acl._propagate_pool = None  # noqa: SLF001
        win_acl._propagate_dispatcher = None  # noqa: SLF001
        win_acl._ensure_propagate_worker()  # noqa: SLF001
        pool = win_acl._propagate_pool  # noqa: SLF001
        assert pool is not None
        assert pool._max_workers == win_acl.ACL_PROPAGATE_WORKERS

    def test_ensure_access_pins_dir_without_waiting(self, tmp_path, monkeypatch):
        target = tmp_path / "ws"
        target.mkdir()
        key = win_acl._propagate_path_key(str(target))  # noqa: SLF001
        with win_acl._propagate_cv:  # noqa: SLF001
            win_acl._propagate_queued.add(key)  # noqa: SLF001
        pinned: list[str] = []
        sync: list[str] = []
        monkeypatch.setattr(win_acl, "_target_has_write_ace", lambda p: False)
        monkeypatch.setattr(
            win_acl, "pin_access_write_ace",
            lambda p: pinned.append(p) or True,
        )
        monkeypatch.setattr(win_acl, "_propagate_tree_now", lambda p: sync.append(p))
        t0 = time.monotonic()
        try:
            win_acl.ensure_access_acl_ready(str(target), timeout=2.0)
        finally:
            with win_acl._propagate_cv:  # noqa: SLF001
                win_acl._propagate_queued.discard(key)  # noqa: SLF001
        assert sync == []
        assert time.monotonic() - t0 < 0.5
        assert any(
            os.path.normcase(os.path.abspath(p))
            == os.path.normcase(str(target.resolve()))
            for p in pinned
        )

    def test_ensure_access_pins_parent_when_target_missing(
        self, tmp_path, monkeypatch,
    ):
        ancestor = tmp_path / "ws"
        ancestor.mkdir()
        missing = ancestor / "new-file.txt"
        pinned: list[str] = []
        sync: list[str] = []
        monkeypatch.setattr(win_acl, "_target_has_write_ace", lambda p: False)
        monkeypatch.setattr(
            win_acl, "pin_access_write_ace",
            lambda p: pinned.append(p) or True,
        )
        monkeypatch.setattr(win_acl, "_propagate_tree_now", lambda p: sync.append(p))
        t0 = time.monotonic()
        win_acl.ensure_access_acl_ready(str(missing), timeout=2.0)
        assert sync == []
        assert time.monotonic() - t0 < 0.5
        assert any(
            os.path.normcase(os.path.abspath(p))
            == os.path.normcase(str(ancestor.resolve()))
            for p in pinned
        )

    def test_ensure_access_pins_file_and_parent(self, tmp_path, monkeypatch):
        parent = tmp_path / "ws"
        parent.mkdir()
        target = parent / "a.txt"
        target.write_text("x", encoding="utf-8")
        pinned: list[str] = []
        monkeypatch.setattr(win_acl, "_target_has_write_ace", lambda p: False)
        monkeypatch.setattr(
            win_acl, "pin_access_write_ace",
            lambda p: pinned.append(p) or True,
        )
        win_acl.ensure_access_acl_ready(str(target), timeout=1)
        keys = {os.path.normcase(os.path.abspath(p)) for p in pinned}
        assert os.path.normcase(str(target.resolve())) in keys
        assert os.path.normcase(str(parent.resolve())) in keys

    def test_ensure_access_skips_when_probe_has_ace(self, tmp_path, monkeypatch):
        target = tmp_path / "ws"
        target.mkdir()
        pinned: list[str] = []
        sync: list[str] = []
        monkeypatch.setattr(win_acl, "_target_has_write_ace", lambda p: True)
        monkeypatch.setattr(
            win_acl, "pin_access_write_ace",
            lambda p: pinned.append(p) or True,
        )
        monkeypatch.setattr(win_acl, "_propagate_tree_now", lambda p: sync.append(p))
        win_acl.ensure_access_acl_ready(str(target), timeout=2.0)
        assert pinned == []
        assert sync == []

    def test_pin_access_write_ace_uses_ancestor_cap_sid(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        nested = root / "child"
        nested.mkdir(parents=True)
        monkeypatch.setattr(win_acl, "_is_acl_forbidden_path", lambda p: False)
        monkeypatch.setattr(win_acl, "_target_has_write_ace", lambda p: False)

        def fake_cap(path: str) -> str | None:
            key = os.path.normcase(os.path.abspath(path))
            if key == os.path.normcase(str(root.resolve())):
                return "S-1-5-21-1-2-3-4"
            return None

        monkeypatch.setattr(
            "jiuwenbox.supervisor.win_setup.get_write_cap_sid_if_exists",
            fake_cap,
        )
        monkeypatch.setattr(
            "jiuwenbox.supervisor.win_setup.get_sandbox_group_sid",
            lambda: "S-1-5-32-100",
        )
        captured: dict[str, object] = {}

        def fake_replace(path, aces, **kwargs):
            captured["path"] = path
            captured["aces"] = aces
            captured["kwargs"] = kwargs

        monkeypatch.setattr(win_acl, "replace_aces_no_propagate", fake_replace)
        assert win_acl.pin_access_write_ace(str(nested))
        kwargs = captured["kwargs"]
        assert kwargs["propagate"] is False
        assert kwargs["inheritable"] is True
        sids = [ace[0] for ace in captured["aces"]]
        assert "S-1-5-21-1-2-3-4" in sids
        assert "S-1-5-32-100" in sids

    def test_sandbox_manager_pins_after_reconcile(self):
        from jiuwenbox.server.sandbox_manager import SandboxManager

        src = inspect.getsource(SandboxManager._build_access_context)
        assert "_ensure_windows_access_acl" in src
        assert "_reconcile_windows_write_roots" in src
        assert "extra_roots" in src
        assert "write_roots" in src

    def test_target_has_write_ace_requires_cap_and_group(self, tmp_path, monkeypatch):
        target = tmp_path / "ws"
        target.mkdir()
        monkeypatch.setattr(win_acl, "_write_cap_sid_for_path", lambda p: "S-1-5-21-cap")
        monkeypatch.setattr(
            "jiuwenbox.supervisor.win_setup.get_sandbox_group_sid",
            lambda: "S-1-5-32-group",
        )
        monkeypatch.setattr(win_acl, "_resolve_sid", lambda sid: sid)
        monkeypatch.setattr(win_acl, "_sid_dedup_key", lambda sid: str(sid))
        grants: set[str] = {"S-1-5-21-cap"}

        def fake_grant(path, mask, keys, deny_only_sids=None):
            del path, mask, deny_only_sids
            return bool(set(keys) & grants)

        monkeypatch.setattr(win_acl, "effective_grant", fake_grant)
        assert win_acl._target_has_write_ace(str(target)) is False  # noqa: SLF001
        grants.add("S-1-5-32-group")
        assert win_acl._target_has_write_ace(str(target)) is True  # noqa: SLF001

    def test_skip_existing_root_ace_does_not_requeue_tree(self):
        src = inspect.getsource(win_acl.apply_sandbox_acl)
        assert "复用上次授权" in src
        assert "schedule_tree_propagate(expanded)" not in src

    def test_lifespan_does_not_revoke_stale_acl(self):
        from jiuwenbox.server import app as app_mod

        src = inspect.getsource(app_mod.lifespan)
        assert "revoke_stale_acl" not in src
        assert "复用上次" in src or "不差集撤销" in src


class TestReconcileUnion:
    def test_reconcile_applies_union_and_revokes_stale(self, monkeypatch):
        monkeypatch.setattr(win_acl, "_require_windows", lambda: None)
        applied: list[list[str]] = []
        revoked: list[list[str]] = []

        monkeypatch.setattr(win_acl, "filter_write_roots", lambda paths, **kw: list(paths))
        monkeypatch.setattr(win_acl, "_path_key", lambda p: os.path.normcase(p))

        def fake_apply(workspace, allow_write, deny_write, **kwargs):
            applied.append(list(allow_write))
            result = MagicMock()
            result.failed = []
            result.paths = list(allow_write)
            return result

        monkeypatch.setattr(win_acl, "apply_sandbox_acl", fake_apply)
        monkeypatch.setattr(
            win_acl, "revoke_write_root_aces",
            lambda paths, sandbox_user_sid=None: revoked.append(list(paths)),
        )
        import jiuwenbox.supervisor.win_setup as win_setup

        monkeypatch.setattr(win_setup, "migrate_legacy_synthetic_aces", lambda sid=None: None)
        monkeypatch.setattr(win_setup, "load_live_write_acl", lambda: [r"C:\old"])
        saved: list[list[str]] = []
        monkeypatch.setattr(
            win_setup, "save_live_write_acl", lambda paths: saved.append(list(paths)),
        )

        win_acl.reconcile_live_write_roots(
            [r"C:\a", r"C:\b"], sandbox_user_sid="S-1-5-21-1-2-3",
        )
        assert applied
        assert r"C:\a" in applied[0] and r"C:\b" in applied[0]
        assert revoked
        assert r"C:\old" in revoked[0]
        # 新清单只留当前存活写根.
        assert saved and sorted(saved[-1]) == [r"C:\a", r"C:\b"]

    def test_reconcile_reads_write_roots_not_all_acl_paths(self, monkeypatch):
        """reconcile 只能看 live write_acl; 读 read_acl 会把 deny_read 的 ACE 撤掉."""
        monkeypatch.setattr(win_acl, "_require_windows", lambda: None)
        revoked: list[list[str]] = []
        monkeypatch.setattr(win_acl, "filter_write_roots", lambda paths, **kw: list(paths))
        monkeypatch.setattr(win_acl, "_path_key", lambda p: os.path.normcase(p))
        monkeypatch.setattr(
            win_acl, "revoke_write_root_aces",
            lambda paths, sandbox_user_sid=None: revoked.append(list(paths)),
        )
        import jiuwenbox.supervisor.win_setup as win_setup

        monkeypatch.setattr(win_setup, "migrate_legacy_synthetic_aces", lambda sid=None: None)
        monkeypatch.setattr(win_setup, "load_live_write_acl", lambda: [r"C:\ws"])
        monkeypatch.setattr(win_setup, "save_live_write_acl", lambda paths: None)

        def _boom(*a, **k):
            raise AssertionError("reconcile 不应读 read_acl / 全量施加路径")

        monkeypatch.setattr(win_setup, "_load_all_applied_paths", _boom)

        win_acl.reconcile_live_write_roots([r"C:\ws"], sandbox_user_sid=None)
        assert revoked == []

    def test_reconcile_raises_when_apply_fails(self, monkeypatch):
        """写 ACE 失败必须 fail closed, 否则调用方拿着无效授权继续跑."""
        monkeypatch.setattr(win_acl, "_require_windows", lambda: None)
        monkeypatch.setattr(win_acl, "filter_write_roots", lambda paths, **kw: list(paths))
        monkeypatch.setattr(win_acl, "_path_key", lambda p: os.path.normcase(p))

        def fake_apply(workspace, allow_write, deny_write, **kwargs):
            result = MagicMock()
            result.failed = list(allow_write)
            result.paths = []
            return result

        monkeypatch.setattr(win_acl, "apply_sandbox_acl", fake_apply)
        import jiuwenbox.supervisor.win_setup as win_setup

        monkeypatch.setattr(win_setup, "migrate_legacy_synthetic_aces", lambda sid=None: None)
        monkeypatch.setattr(win_setup, "load_live_write_acl", lambda: [])
        monkeypatch.setattr(win_setup, "save_live_write_acl", lambda paths: None)

        with pytest.raises(win_acl.WriteAceFailed):
            win_acl.reconcile_live_write_roots([r"C:\new"], sandbox_user_sid=None)

    def test_reconcile_leftover_root_failure_does_not_block_required(self, monkeypatch):
        """Union 里的 C:\\tmp 打 ACE 失败, 不应 500 掉这次工作区 write_file."""
        monkeypatch.setattr(win_acl, "_require_windows", lambda: None)
        monkeypatch.setattr(win_acl, "filter_write_roots", lambda paths, **kw: list(paths))
        monkeypatch.setattr(win_acl, "_path_key", lambda p: os.path.normcase(p))

        def fake_apply(workspace, allow_write, deny_write, **kwargs):
            result = MagicMock()
            result.failed = [
                p for p in allow_write
                if os.path.normcase(p) == os.path.normcase(r"C:\tmp")
            ]
            result.paths = [p for p in allow_write if p not in result.failed]
            return result

        monkeypatch.setattr(win_acl, "apply_sandbox_acl", fake_apply)
        import jiuwenbox.supervisor.win_setup as win_setup

        monkeypatch.setattr(win_setup, "migrate_legacy_synthetic_aces", lambda sid=None: None)
        monkeypatch.setattr(win_setup, "load_live_write_acl", lambda: [])
        monkeypatch.setattr(win_setup, "save_live_write_acl", lambda paths: None)

        failed = win_acl.reconcile_live_write_roots(
            [r"C:\ws", r"C:\tmp"],
            sandbox_user_sid=None,
            required_roots=[r"C:\ws"],
        )
        assert [os.path.normcase(p) for p in failed] == [os.path.normcase(r"C:\tmp")]


class TestNoImplicitDataAce:
    def test_apply_desktop_data_rw_removed(self):
        assert not hasattr(win_acl, "apply_desktop_data_rw")

    def test_apply_sandbox_acl_has_no_workspace_fallback(self):
        src = inspect.getsource(win_acl.apply_sandbox_acl)
        assert "write_targets = filter_write_roots(" in src
        assert "write_targets = allow_write or [workspace]" not in src
        assert "JIUWENBOX_HOME" not in src

    def test_process_create_does_not_append_env_acl(self):
        from jiuwenbox.server.runtime import process as process_mod

        src = inspect.getsource(process_mod.ProcessRuntime)
        assert "allow_write_paths.append" not in src
        assert "combined_allow_write" in src
        assert "allow_read_paths.append" not in src
        assert "apply_desktop_data_rw" not in src
        assert "_under_desktop" not in src
        assert "_expand_ws" not in src
        assert "{{ workspace }}" not in src

    def test_parent_traverse_plan_is_traverse_only(self):
        from jiuwenbox.supervisor import win_constants as const

        plan = win_acl._parent_traverse_plan()  # noqa: SLF001
        assert int(plan["rights"]) == const.FILE_GENERIC_EXECUTE

    def test_write_trustees_not_user_sid(self):
        src = inspect.getsource(win_acl.apply_sandbox_acl)
        assert "_write_trustees" in src
        assert "_grant_identity_aces" in src
        assert "_grant_our_aces" not in src

    def test_legacy_synth_not_in_token(self):
        from jiuwenbox.supervisor import win_exec

        src = inspect.getsource(win_exec._create_restricted_token)  # noqa: SLF001
        assert "get_synthetic_write_sid" not in src

    def test_migrate_legacy_is_one_shot(self):
        from jiuwenbox.supervisor import win_setup

        src = inspect.getsource(win_setup.migrate_legacy_synthetic_aces)
        assert "migrated_legacy_synth" in src
        assert "purge_sid_aces" in src


class TestAclStateLists:
    """write_acl (live 写根) 与 read_acl (读/Deny) 分桶, 混用会让 reconcile purge Deny ACE."""

    def test_state_has_write_and_read_buckets(self):
        from jiuwenbox.supervisor import win_setup

        state = win_setup._empty_acl_state()  # noqa: SLF001
        assert state["version"] == 3
        assert state["write_acl"] == {}
        assert state["read_acl"] == []

    def test_v1_state_migrates_into_read_acl_only(self, monkeypatch, tmp_path):
        from jiuwenbox.supervisor import win_setup

        f = tmp_path / "acl_state.json"
        f.write_text(json.dumps({
            "version": 1,
            "applied_write_roots": [r"C:\ws", r"C:\secrets"],
        }), encoding="utf-8")
        monkeypatch.setattr(win_setup, "_acl_state_file", lambda: f)
        monkeypatch.setattr(win_setup, "_acl_home", lambda: tmp_path)
        monkeypatch.setattr(win_setup, "reg_get_str", lambda *a, **k: "")

        state = win_setup._load_acl_state_unlocked()  # noqa: SLF001
        # 旧清单是「全部施加路径」, 不能当 live 写根.
        assert state["read_acl"] == [r"C:\ws", r"C:\secrets"]
        assert state["write_acl"] == {}

    def test_v2_state_splits_write_and_read(self, monkeypatch, tmp_path):
        from jiuwenbox.supervisor import win_setup

        f = tmp_path / "acl_state.json"
        f.write_text(json.dumps({
            "version": 2,
            "write_cap_by_path": {r"C:\ws": "S-1-5-21-1-2-3-4"},
            "applied_write_roots": [r"C:\ws"],
            "applied_acl_paths": [r"C:\ws", r"C:\secrets"],
        }), encoding="utf-8")
        monkeypatch.setattr(win_setup, "_acl_state_file", lambda: f)
        monkeypatch.setattr(win_setup, "_acl_home", lambda: tmp_path)
        monkeypatch.setattr(win_setup, "reg_get_str", lambda *a, **k: "")

        state = win_setup._load_acl_state_unlocked()  # noqa: SLF001
        assert state["read_acl"] == [r"C:\secrets"]
        write_entries = list(state["write_acl"].values())
        assert len(write_entries) == 1
        assert write_entries[0]["cap_sid"] == "S-1-5-21-1-2-3-4"
        assert write_entries[0]["live"] is True

    def test_process_records_sandbox_acl_buckets(self):
        from jiuwenbox.server.runtime import process as process_mod

        src = inspect.getsource(process_mod.ProcessRuntime)
        assert "record_sandbox_acl(" in src
        assert "write_paths=list(allow_write_paths)" in src

    def test_v3_state_roundtrip(self, monkeypatch, tmp_path):
        from jiuwenbox.supervisor import win_setup

        f = tmp_path / "acl_state.json"
        payload = {
            "version": 3,
            "write_acl": {
                "c:/ws": {
                    "cap_sid": "S-1-5-21-1-2-3-4",
                    "live": True,
                    "path": r"C:\ws",
                },
            },
            "read_acl": [r"C:\secrets"],
            "readonly_cap": "",
            "migrated_legacy_synth": False,
        }
        f.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(win_setup, "_acl_state_file", lambda: f)
        monkeypatch.setattr(win_setup, "_acl_home", lambda: tmp_path)
        monkeypatch.setattr(win_setup, "reg_get_str", lambda *a, **k: "")

        state = win_setup._load_acl_state_unlocked()  # noqa: SLF001
        assert state["read_acl"] == [r"C:\secrets"]
        assert win_setup.load_live_write_acl() == [r"C:\ws"]
        assert win_setup.get_write_cap_sid_if_exists(r"C:\ws") == "S-1-5-21-1-2-3-4"

    def test_bundled_policy_has_no_workspace_placeholder(self):
        """写根由 sysop_builder 以真实路径 append, 基底 policy 不含 {{ workspace }}."""
        import jiuwenbox

        cfg = Path(jiuwenbox.__file__).parent / "configs" / "windows-policy.yaml"
        text = cfg.read_text(encoding="utf-8")
        fs = yaml.safe_load(text)["windows"]["filesystem"]
        assert "{{ workspace }}" not in text
        assert "{{ workspace }}" not in (fs.get("allow_write") or [])
        assert "{{ workspace }}" not in (fs.get("workspace") or [])
        assert "workspace" in fs

    def test_revoke_sandbox_acl_targets_group_and_cap_sids(self):
        """写 ACE 的 trustee 是组 + cap SID; 不带上就撤不干净."""
        src = inspect.getsource(win_acl.revoke_sandbox_acl)
        assert "get_sandbox_group_sid" in src
        assert "get_write_cap_sid_if_exists" in src


class TestRootPolicyAclAtStartup:
    def test_drop_root_policy_acl_paths_skips_remembered(self):
        from jiuwenbox.supervisor import win_setup

        win_setup._remember_root_policy_acl_paths([r"C:\Users\test"])  # noqa: SLF001
        try:
            out = win_setup.drop_root_policy_acl_paths([
                r"C:\Users\test",
                r"C:\Users\test\.jiuwenbox\workspace\abc",
            ])
            keys = {os.path.normcase(os.path.abspath(p)) for p in out}
            assert os.path.normcase(os.path.abspath(r"C:\Users\test")) not in keys
            assert any("workspace" in p.replace("\\", "/") for p in out)
        finally:
            win_setup._remember_root_policy_acl_paths([])  # noqa: SLF001

    def test_apply_root_policy_acl_grants_then_create_drops(
        self, monkeypatch,
    ):
        from types import SimpleNamespace
        from jiuwenbox.supervisor import win_setup, win_acl
        from jiuwenbox.supervisor.win_acl import ApplyAclResult

        monkeypatch.setattr(win_setup, "_require_windows", lambda: None)
        monkeypatch.setattr(win_setup, "get_sandbox_user_sid", lambda: "S-1-5-21-1")
        monkeypatch.setattr(win_setup, "get_sandbox_group_sid", lambda: None)
        monkeypatch.setattr(win_setup, "get_preinstalled_read_paths", lambda: set())
        monkeypatch.setattr(win_setup, "record_sandbox_acl", lambda **kw: None)
        applied: list[list[str]] = []

        def fake_apply(workspace, allow_write, deny_write, **kwargs):
            applied.append(list(allow_write))
            result = ApplyAclResult()
            result.paths = list(allow_write)
            return result

        monkeypatch.setattr(win_acl, "apply_sandbox_acl", fake_apply)
        monkeypatch.setattr(win_acl, "grant_parent_traverse", lambda *a, **k: None)
        policy = SimpleNamespace(windows=SimpleNamespace(filesystem=SimpleNamespace(
            allow_write=[r"C:\Users\test"],
            workspace=[r"C:\ws"],
            deny_write=[],
            allow_read=[],
            deny_read=[],
        )))
        try:
            win_setup.apply_root_policy_acl(policy)
            assert applied == [[r"C:\Users\test", r"C:\ws"]]
            dropped = win_setup.drop_root_policy_acl_paths(
                [r"C:\Users\test", r"D:\project"],
            )
            assert dropped == [r"D:\project"]
        finally:
            win_setup._remember_root_policy_acl_paths([])  # noqa: SLF001

    def test_schedule_remembers_before_apply(self, monkeypatch):
        from types import SimpleNamespace
        from jiuwenbox.supervisor import win_setup

        started = threading.Event()
        proceed = threading.Event()

        def slow_apply(_policy):
            started.set()
            proceed.wait(timeout=2)

        monkeypatch.setattr(win_setup, "apply_root_policy_acl", slow_apply)
        policy = SimpleNamespace(windows=SimpleNamespace(filesystem=SimpleNamespace(
            allow_write=[r"C:\Users\test"],
            deny_write=[],
            allow_read=[],
            deny_read=[],
        )))
        try:
            win_setup.schedule_root_policy_acl(policy)
            dropped = win_setup.drop_root_policy_acl_paths(
                [r"C:\Users\test", r"D:\project"],
            )
            assert dropped == [r"D:\project"]
            assert started.wait(timeout=2.0)
        finally:
            proceed.set()
            win_setup._remember_root_policy_acl_paths([])  # noqa: SLF001


class TestAuthAndListenPort:
    def test_missing_bearer_token_is_401(self, monkeypatch):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        monkeypatch.setenv("JIUWENBOX_API_TOKEN", "secret-token")

        async def ok(_request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/health", ok)])
        app.add_middleware(BearerTokenAuthMiddleware)
        client = TestClient(app)
        denied = client.get("/health")
        assert denied.status_code == 401
        allowed = client.get("/health", headers={"Authorization": "Bearer secret-token"})
        assert allowed.status_code == 200

    def test_listen_port_inside_proxy_range_refused(self, monkeypatch):
        from jiuwenbox.server.app import _assert_listen_port_outside_proxy_range

        monkeypatch.setenv("JIUWENBOX_LISTEN", "http://127.0.0.1:60085")
        with pytest.raises(RuntimeError, match="inside windows.proxy"):
            _assert_listen_port_outside_proxy_range(60080, 60089)

    def test_listen_port_outside_proxy_range_ok(self, monkeypatch):
        from jiuwenbox.server.app import _assert_listen_port_outside_proxy_range

        monkeypatch.setenv("JIUWENBOX_LISTEN", "http://127.0.0.1:8321")
        _assert_listen_port_outside_proxy_range(60080, 60089)

    def test_sandbox_env_drops_api_token(self):
        from jiuwenbox.server.runtime import process as process_mod

        src = inspect.getsource(process_mod.ProcessRuntime)
        assert 'env.pop("JIUWENBOX_API_TOKEN", None)' in src
        assert '_k != "JIUWENBOX_API_TOKEN"' in src
