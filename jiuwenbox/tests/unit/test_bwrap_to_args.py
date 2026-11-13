# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for :meth:`BwrapConfig.to_args` bind emission ordering.

These tests pin the depth-sorted bind emission: all binds are emitted in one
global stable sort by destination path depth (shallowest first, ro before rw
on ties). Depth order is a topological order of the bind tree, so under
bwrap's later-overrides-earlier semantics no mount is shadowed by an
ancestor, regardless of the order producers appended the entries.

The tests construct :class:`BwrapConfig` directly (no :class:`SecurityPolicy`
/ pwd / grp resolution) so they run on any host without a sandbox runtime.
"""

from __future__ import annotations

from jiuwenbox.supervisor.bwrap import BwrapConfig

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


def _bind_order(args: list[str], flag: str) -> list[str]:
    """Destination paths of all ``flag`` binds, in emission order."""
    return [args[i + 2] for i in range(len(args) - 2) if args[i] == flag]


# ---------------------------------------------------------------------------
# Emission ordering
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
    # A ro-bind at a shallower depth than the rw binds must stay emitted
    # before them, preserving the bind_root_entries-vs-bind_mounts override
    # contract (notably code-agent's ro root entries + rw child dirs).
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
    # One shallow ro (/bin), one deep ro (config.yaml under ~/.jiuwenswarm rw).
    # The shallow ro must come before rw; the deep ro must come after its
    # rw ancestor.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/h/bin", "/bin"), ("/h/cfg", CONFIG_YAML)]
    cfg.rw_binds = [("/h/ws", JIUWENSWARM)]
    args = cfg.to_args()

    ro_bin = _bind_index(args, "--ro-bind", "/bin")
    rw_parent = _bind_index(args, "--bind", JIUWENSWARM)
    ro_cfg = _bind_index(args, "--ro-bind", CONFIG_YAML)
    assert ro_bin >= 0 and rw_parent >= 0 and ro_cfg >= 0
    assert ro_bin < rw_parent < ro_cfg


def test_sibling_prefix_not_reordered():
    # /homeless shares the "/home" string prefix but is a sibling at the same
    # depth; the stable sort keeps declaration order among equal depths.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/h/x", "/homeless")]
    cfg.rw_binds = [("/h/backing", HOME)]
    args = cfg.to_args()

    ro_homeless = _bind_index(args, "--ro-bind", "/homeless")
    home_bind = _bind_index(args, "--bind", HOME)
    assert ro_homeless >= 0 and home_bind >= 0
    # Same depth (1): ro before rw via the tie rule.
    assert ro_homeless < home_bind


def test_same_path_ro_not_nested_remount_still_fires():
    # A ro-bind whose dst equals a rw bind's dst is a depth tie, not a
    # nesting; the ro stays before the rw bind. The rw bind then wins the
    # mountpoint and the trailing --remount-ro flips it back to read-only --
    # this is the same-path belt-and-suspenders path that remount-ro *can*
    # handle (a live mountpoint, not a shadowed one).
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


def test_nested_rw_under_home_lifecycle_emitted_after_home_backing():
    # The directories:/home backing bind is appended after the policy's
    # bind_mounts; depth sorting keeps the per-path bind live regardless of
    # producer order.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.rw_binds = [(JIUWENSWARM, JIUWENSWARM), (HOME, HOME)]
    args = cfg.to_args()

    home_bind = _bind_index(args, "--bind", HOME)
    ws_bind = _bind_index(args, "--bind", JIUWENSWARM)
    assert home_bind >= 0 and ws_bind >= 0
    assert home_bind < ws_bind


def test_backing_then_nested_rw_then_nested_ro_order():
    # directories:/home backing + bind_mounts underneath it + a nested ro bind.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/host/cfg", CONFIG_YAML), ("/host/bin", "/bin")]
    cfg.rw_binds = [
        (JIUWENSWARM, JIUWENSWARM),
        ("/host/backing-home", HOME),
        ("/host/backing-tmp", "/tmp"),
    ]
    cfg.remount_ro = ["/", "/bin", CONFIG_YAML]
    args = cfg.to_args()

    ro_bin = _bind_index(args, "--ro-bind", "/bin")
    home_bind = _bind_index(args, "--bind", HOME)
    tmp_bind = _bind_index(args, "--bind", "/tmp")
    ws_bind = _bind_index(args, "--bind", JIUWENSWARM)
    ro_cfg = _bind_index(args, "--ro-bind", CONFIG_YAML)
    assert -1 not in (ro_bin, home_bind, tmp_bind, ws_bind, ro_cfg)
    assert ro_bin < home_bind < tmp_bind < ws_bind < ro_cfg
    assert _remount_index(args, CONFIG_YAML) > ro_cfg


# ---------------------------------------------------------------------------
# Topological-order invariants (review findings !9773)
# ---------------------------------------------------------------------------


def test_deep_rw_chain_declared_deep_to_shallow_is_depth_sorted():
    # Review finding: a 3-level rw chain declared deep-to-shallow must still
    # be emitted shallowest-first; only the partition approach left the
    # nested group in producer order, letting a late shallow bind shadow a
    # deeper one. The global depth sort closes this by construction.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.rw_binds = [
        ("/h/c", "/home/a/b/c"),
        ("/h/b", "/home/a/b"),
        ("/h/a", "/home/a"),
    ]
    args = cfg.to_args()
    assert _bind_order(args, "--bind") == ["/home/a", "/home/a/b", "/home/a/b/c"]


def test_ro_parent_with_rw_child_keeps_child_flip():
    # Review finding: a ro bind that is itself nested under a rw ancestor
    # while carrying its own rw child must NOT be emitted after that child.
    # Depth order guarantees rw-ancestor -> ro-parent -> rw-child, so the
    # explicit rw child still flips the subtree to read-write.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/ro/ws", "/home/user/ws")]
    cfg.rw_binds = [
        ("/rw/sub", "/home/user/ws/sub"),
        ("/rw/backing", HOME),
    ]
    args = cfg.to_args()

    backing = _bind_index(args, "--bind", HOME)
    ro_parent = _bind_index(args, "--ro-bind", "/home/user/ws")
    rw_child = _bind_index(args, "--bind", "/home/user/ws/sub")
    assert -1 not in (backing, ro_parent, rw_child)
    assert backing < ro_parent < rw_child


def test_nested_ro_group_is_depth_sorted_not_declaration_sorted():
    # Two ro binds under the same rw ancestor, declared deep-to-shallow, must
    # be emitted shallowest-first so the deeper ro mountpoint survives.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.ro_binds = [("/r/deep", "/w/x/y"), ("/r/shallow", "/w/x")]
    cfg.rw_binds = [("/p", "/w")]
    args = cfg.to_args()

    rw_root = _bind_index(args, "--bind", "/w")
    ro_shallow = _bind_index(args, "--ro-bind", "/w/x")
    ro_deep = _bind_index(args, "--ro-bind", "/w/x/y")
    assert -1 not in (rw_root, ro_shallow, ro_deep)
    assert rw_root < ro_shallow < ro_deep


def test_unnormalized_paths_do_not_break_ancestor_ordering():
    # ``_expand_path`` (expandvars/expanduser) does not normalize ``//`` or
    # trailing slashes; depth counting must still order a (logical) child
    # after its ancestor. A trailing slash does not change the segment count,
    # and ``//`` collapses in the split, so both stay monotone.
    cfg = BwrapConfig(command=["/bin/sh"])
    cfg.rw_binds = [
        ("/h/ws", "/home/user/ws/"),
        ("/h/backing", "/home//"),
    ]
    args = cfg.to_args()

    backing = _bind_index(args, "--bind", "/home//")
    ws = _bind_index(args, "--bind", "/home/user/ws/")
    assert backing >= 0 and ws >= 0
    assert backing < ws
