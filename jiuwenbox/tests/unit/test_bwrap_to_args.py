# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for :meth:`BwrapConfig.to_args` bind emission ordering.

These tests pin the fix for the bug where a read-only bind nested *under* a
read-write parent bind (e.g. ``config.yaml`` under a rw bind of its parent
``~/.jiuwenswarm`` or under the ``/home`` lifecycle rw bind) was emitted
before the rw parent, got shadowed, and the trailing ``--remount-ro`` then
failed with ``realpath(destination): No such file or directory``.

The tests construct :class:`BwrapConfig` directly (no :class:`SecurityPolicy`
/ pwd / grp resolution) so they run on any host without a sandbox runtime.
"""

from __future__ import annotations

from jiuwenbox.supervisor.bwrap import (
    BwrapConfig,
    _is_strict_subpath,
    _partition_nested_ro_binds,
)

CONFIG_YAML = "/home/user/.jiuwenswarm/config/config.yaml"
JIUWENSWARM = "/home/user/.jiuwenswarm"
HOME = "/home"
AGENT = "/home/user/.jiuwenswarm/service_default/agent_default/agent"


def _bind_index(args: list[str], flag: str, dst: str) -> int:
    """Index ``i`` where ``args[i]==flag`` and ``args[i+2]==dst``.

    bwrap bind flags (``--ro-bind`` / ``--bind`` / ``--dev-bind``) take two
    operands: ``flag src dst``. Returns -1 if not found.
    """
    for i in range(len(args) - 2):
        if args[i] == flag and args[i + 2] == dst:
            return i
    return -1


def _remount_index(args: list[str], dst: str) -> int:
    """Index ``i`` where ``args[i]=='--remount-ro'`` and ``args[i+1]==dst``."""
    for i in range(len(args) - 1):
        if args[i] == "--remount-ro" and args[i + 1] == dst:
            return i
    return -1


# ---------------------------------------------------------------------------
# Helper unit tests (white-box)
# ---------------------------------------------------------------------------


def test_is_strict_subpath_descendant_and_boundary_cases():
    assert _is_strict_subpath("/home/user/x", "/home") is True
    # equal paths are NOT strict subpaths
    assert _is_strict_subpath("/home", "/home") is False
    # a sibling sharing a string prefix must NOT be misclassified
    assert _is_strict_subpath("/homeless", "/home") is False
    # root ancestor is special-cased away (root binds are deduplicated upstream)
    assert _is_strict_subpath("/home/user/x", "/") is False
    # normalization defends against trailing slashes and doubled separators
    assert _is_strict_subpath("/home//user/x", "/home") is True
    assert _is_strict_subpath("/home/user/x/", "/home/") is True


def test_partition_nested_ro_binds_splits_and_preserves_order():
    ro = [("/h/bin", "/bin"), ("/h/cfg", CONFIG_YAML), ("/h/opt", "/opt")]
    rw = [("/h/ws", JIUWENSWARM)]
    non_nested, nested = _partition_nested_ro_binds(ro, rw)
    assert non_nested == [("/h/bin", "/bin"), ("/h/opt", "/opt")]
    assert nested == [("/h/cfg", CONFIG_YAML)]


def test_partition_nested_ro_binds_home_ancestor_from_lifecycle_dirs():
    # /home is a rw ancestor of config.yaml (ProcessRuntime binds the empty
    # lifecycle /home backing dir). This is the case that defeats naive
    # "preserve producer order" fixes (A') because /home is appended by the
    # runtime, not by sysop_builder.
    ro = [("/h/cfg", CONFIG_YAML)]
    rw = [("/h/backing", HOME)]
    non_nested, nested = _partition_nested_ro_binds(ro, rw)
    assert non_nested == []
    assert nested == [("/h/cfg", CONFIG_YAML)]


# ---------------------------------------------------------------------------
# to_args emission ordering
# ---------------------------------------------------------------------------


def test_nested_ro_emitted_after_rw_parent_and_remount_resolves():
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/h/cfg", CONFIG_YAML)]
    cfg.rw_binds = [("/h/ws", JIUWENSWARM)]
    cfg.remount_ro = [CONFIG_YAML]
    args = cfg.to_args()

    rw_parent = _bind_index(args, "--bind", JIUWENSWARM)
    ro_cfg = _bind_index(args, "--ro-bind", CONFIG_YAML)
    remount_cfg = _remount_index(args, CONFIG_YAML)
    assert rw_parent >= 0 and ro_cfg >= 0 and remount_cfg >= 0
    assert rw_parent < ro_cfg < remount_cfg


def test_nested_ro_under_home_lifecycle_emitted_after_home_rw():
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/h/cfg", CONFIG_YAML)]
    cfg.rw_binds = [("/h/backing", HOME)]
    cfg.remount_ro = [CONFIG_YAML]
    args = cfg.to_args()

    home_bind = _bind_index(args, "--bind", HOME)
    ro_cfg = _bind_index(args, "--ro-bind", CONFIG_YAML)
    remount_cfg = _remount_index(args, CONFIG_YAML)
    assert home_bind >= 0 and ro_cfg >= 0 and remount_cfg >= 0
    assert home_bind < ro_cfg < remount_cfg


def test_non_nested_ro_keeps_pre_rw_order():
    # A ro-bind NOT under any rw parent must stay emitted before rw binds,
    # preserving the bind_root_entries-vs-bind_mounts override contract for
    # non-overlapping paths (notably code-agent's ro root entries + rw child dirs).
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/h/bin", "/bin"), ("/h/opt", "/opt")]
    cfg.rw_binds = [("/h/ws", AGENT)]
    args = cfg.to_args()

    ro_bin = _bind_index(args, "--ro-bind", "/bin")
    ro_opt = _bind_index(args, "--ro-bind", "/opt")
    rw_agent = _bind_index(args, "--bind", AGENT)
    assert ro_bin >= 0 and ro_opt >= 0 and rw_agent >= 0
    assert ro_bin < rw_agent
    assert ro_opt < rw_agent


def test_mixed_nested_and_non_nested_ro_ordering():
    # One non-nested ro (/bin), one nested ro (config.yaml under ~/.jiuwenswarm rw).
    # Non-nested ro must come before rw; nested ro must come after rw.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/h/bin", "/bin"), ("/h/cfg", CONFIG_YAML)]
    cfg.rw_binds = [("/h/ws", JIUWENSWARM)]
    args = cfg.to_args()

    ro_bin = _bind_index(args, "--ro-bind", "/bin")
    rw_parent = _bind_index(args, "--bind", JIUWENSWARM)
    ro_cfg = _bind_index(args, "--ro-bind", CONFIG_YAML)
    assert ro_bin >= 0 and rw_parent >= 0 and ro_cfg >= 0
    assert ro_bin < rw_parent < ro_cfg


def test_sibling_prefix_not_misclassified_as_nested():
    # /homeless shares the "/home" string prefix with the rw /home bind but is
    # a sibling, not a descendant -- it must NOT be reordered after the rw bind.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/h/x", "/homeless")]
    cfg.rw_binds = [("/h/backing", HOME)]
    args = cfg.to_args()

    ro_homeless = _bind_index(args, "--ro-bind", "/homeless")
    home_bind = _bind_index(args, "--bind", HOME)
    assert ro_homeless >= 0 and home_bind >= 0
    assert ro_homeless < home_bind


def test_same_path_ro_not_nested_remount_still_fires():
    # A ro-bind whose dst equals a rw bind's dst is NOT a strict subpath, so it
    # stays in the non-nested partition (emitted before the rw bind). The rw
    # bind then wins the mountpoint and the trailing --remount-ro flips it back
    # to read-only -- this is the same-path belt-and-suspenders path that
    # remount-ro *can* handle (a live mountpoint, not a shadowed one).
    cfg = BwrapConfig(command=["/bin/sh"])
    same = "/srv/secret"
    cfg.ro_binds = [("/h/ro", same)]
    cfg.rw_binds = [("/h/rw", same)]
    cfg.remount_ro = [same]
    args = cfg.to_args()

    ro = _bind_index(args, "--ro-bind", same)
    rw = _bind_index(args, "--bind", same)
    remount = _remount_index(args, same)
    assert ro >= 0 and rw >= 0 and remount >= 0
    assert ro < rw < remount
