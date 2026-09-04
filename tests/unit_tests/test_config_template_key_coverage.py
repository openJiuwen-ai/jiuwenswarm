# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The default config template must cover the keys the project itself ships.

``jiuwenswarm/resources/config.yaml`` is not only a defaults file. It is the
set of keys ``migrate_config_from_template`` accounts for when it projects an
operator's config through the template: ``_deep_merge`` walks the template's
keys, so an option the template does not name has no default to hand down and
nothing in the merge that knows it exists. That merge runs at import of
``jiuwenswarm/app.py``, ``gateway/app_gateway.py`` and
``server/app_agentserver.py``, which means it runs on every service start.

So an option added to the code without a line in this template is not merely
undocumented. It reaches no fresh installation, because ``prepare_workspace``
copies this file verbatim; it never reaches a config written before the option
existed, because the merge has nothing to add; and it is the shape of key a
merge that cleans up after retired options takes back out of the operator's
file. The losses this module was written after were exactly that, and they
happened quietly, between one restart and the next, in a file the operator had
edited by hand.

The measurement is made against the files themselves rather than by running
the merge. What is being asked is "which options does a shipped config set
that this template fails to account for", and that is a question about the two
documents. Asking it of the files keeps the answer stable whatever the merge
later decides to do with a key it has no default for, and it does not
reproduce the merge's recursion bound: a key deep enough that the merge stops
short is still a key the template does not name.

There is no way to ask the source "which config paths does this read": the
reads are ``dict.get`` calls on sub-dicts assembled at runtime, sometimes
forwarded wholesale into a dataclass or a pydantic model that decides for
itself which names it recognises. What can be checked is the project's own
configuration files. The distributed-team configs and the yuanrong deployment
template are full configs maintained beside the code, and in practice a new
option reaches one of them at about the time it reaches the code. Every key
they carry that the default template omits is therefore either a gap in the
template or a divergence with a reason.

This module asserts the list of divergences exactly, in both directions:
an unexplained one fails, and so does an entry that no longer diverges. The
second half is what stops the list from quietly becoming the specification.

What this does NOT catch, stated plainly so nobody reads more into a pass:

* an option read by the code and written into no shipped config at all --
  nothing here can see it;
* a key only an operator ever sets, which is the case that caused the losses
  this module was written after;
* a key present in the template but read under a different name.

For the neighbouring property -- that an open-ended map is shipped bare rather
than as ``{}`` -- see ``TestTemplatesShipOpenEndedMapsBare`` in
``test_config_open_ended_maps.py``.

One test does run the migration, because a rename is only a rename if the
value arrives. It is given both paths explicitly --
``migrate_config_from_template(template_path, user_config_path)`` -- and
``user_config_path`` is always inside pytest's ``tmp_path``. No test in this
module may call the no-argument ``ensure_config_migrated_from_template()``
instead. That wrapper resolves the running installation's workspace and
rewrites the config file it finds there, so a later edit reaching for the
convenient call would destroy the developer's own configuration -- which is
the defect this module exists to describe.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.config import migrate_config_from_template

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO_ROOT / "jiuwenswarm" / "resources" / "config.yaml"

# Full configs the project ships beside the code. A deployment-specific file is
# as good a witness as a resource one: what matters is that a human maintains
# it against the same code.
_SHIPPED_CONFIGS = (
    "jiuwenswarm/resources/config.team.distributed.leader.yaml",
    "jiuwenswarm/resources/config.team.distributed.teammate.yaml",
    "deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml",
)

# Whole subtrees a shipped config carries and the default template does not,
# for a reason that is not a missing option. Each entry is a path prefix.
_ACCEPTED_SUBTREES: dict[tuple[str, ...], str] = {
    ("channels", "feishu"): (
        "The distributed and yuanrong files configure a single Feishu app with "
        "the fields flat under channels.feishu; the default template nests the "
        "same fields inside an `apps:` list, which the merge hands over whole. "
        "Same options, different nesting."
    ),
    ("channels", "xiaoyi"): (
        "As channels.feishu: flat single-app shape versus the template's "
        "`apps:` list."
    ),
    ("team",): (
        "Top-level `team` is the distributed-transport section, which only a "
        "multi-process deployment has. The single-node template has no use for "
        "it and an operator of one has nothing to lose there."
    ),
    ("sandbox",): (
        "yuanrong-only: the sandbox section describes the YuanRong instance "
        "the gateway runs inside, not an option of this process."
    ),
    ("models", "default"): (
        "A typo in the leader file for `models.defaults`, which the template "
        "does ship. Fixing it belongs to that file, not to this template."
    ),
}

# The three legacy probe fields moved to the health_check section, and the
# template stopped carrying them. migrate_legacy_heartbeat_probe_config exists
# to carry an operator's value across, but it runs after this merge rather than
# before it, so the value is gone by the time it looks. That ordering is a
# defect in its own right and is reported separately; here the keys are listed
# only so this check does not re-report it.
_HEARTBEAT_PROBE_REASON = (
    "Retired from the template in favour of health_check.{every,target,"
    "active_hours}. The yuanrong deployment file still carries the old names. "
    "Carrying an operator's legacy value across is tracked separately."
)

# The distributed-team shape of modes.team.jiuwen_team. The default template
# ships the single-node variant of each of these -- one leader, inprocess
# transport, sqlite storage with no params, a workspace with no path -- so the
# multi-process settings have no counterpart to merge into. Listed key by key
# rather than exempting modes.team.jiuwen_team wholesale, because that subtree
# does hold options the single-node template owes the operator,
# enable_permissions among them.
_DISTRIBUTED_TEAM_SHAPE: dict[tuple[str, ...], str] = {
    ("modes", "team", "jiuwen_team", "agents", "teammate"): (
        "Only a distributed team has a teammate agent spec; the single-node "
        "template ships agents.leader alone."
    ),
    ("modes", "team", "jiuwen_team", "storage", "params"): (
        "postgresql connection settings. The default template ships "
        "storage.type: sqlite, which takes no params."
    ),
    ("modes", "team", "jiuwen_team", "transport", "params"): (
        "pyzmq endpoint settings. The default template ships "
        "transport.type: inprocess, which takes no params."
    ),
    ("modes", "team", "jiuwen_team", "workspace", "root_path"): (
        "The shared workspace root only a multi-process team needs; an "
        "inprocess team uses the agent workspace."
    ),
    ("modes", "team", "jiuwen_team", "workspace", "version_control"): (
        "config_loader.setdefault()s this to False, which is also what the "
        "distributed file sets, so the default template has nothing to add."
    ),
    ("modes", "team", "jiuwen_team", "memory", "timezone_offset_hours"): (
        "Set only by the distributed leader; no reader in this tree."
    ),
}

# Individual keys, same rule.
_ACCEPTED_KEYS: dict[tuple[str, ...], str] = {
    ("react", "context_engine_config", "enable_kv_cache_release"): (
        "Misplaced in the yuanrong file. The key the code reads is "
        "react.kv_cache_affinity_config.enable_kv_cache_release, which the "
        "template ships; the context_engine_config copy is read by nothing."
    ),
    ("symphony", "fingerprint", "normalization"): (
        "Retired. SymphonyConfig builds `fingerprint` from `scan` and "
        "`extraction` only; no reader in this tree, in openjiuwen, or in the "
        "frontend."
    ),
    ("symphony", "skill_retrieval", "retrieve"): (
        "Retired. Nothing fetches skill_retrieval.retrieve; the block survives "
        "only where a writer round-trips the file it sits in."
    ),
    ("modes", "team", "jiuwen_team", "enable_permissions"): (
        "Retired deliberately. The team-level switch was removed from this "
        "template when _resolve_enable_permissions stopped reading it and took "
        "the team's setting from the global permissions.enabled instead. The "
        "yuanrong deployment file still carries the old key; it controls "
        "nothing, so the template does not owe it to an operator."
    ),
    ("heartbeat", "every"): _HEARTBEAT_PROBE_REASON,
    ("heartbeat", "target"): _HEARTBEAT_PROBE_REASON,
    ("heartbeat", "active_hours"): _HEARTBEAT_PROBE_REASON,
    **_DISTRIBUTED_TEAM_SHAPE,
}

# Keys a pre-merge migration renames rather than drops. Listed with where the
# value must land, so the assertion is that the migration worked -- not merely
# that the old name is allowed to disappear.
_RENAMED_BY_MIGRATION: dict[tuple[str, ...], tuple[str, ...]] = {
    ("modes", "agent", "plan"): ("modes", "agent", "memory"),
    ("modes", "agent", "fast"): ("modes", "agent", "memory"),
}


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _unaccounted(
    template: dict, shipped: dict, prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Keys ``shipped`` sets that ``template`` does not account for.

    Walks the two documents together the way ``_deep_merge`` walks them. A key
    the template does not name is reported and not descended into, so what
    comes back is the shallowest path of each divergent subtree. Below a key
    both documents name, the walk continues only where both values are
    mappings -- which is the only case the merge descends into either. A
    template value that is not a mapping, or a list on either side, makes the
    merge take the operator's value verbatim, so nothing beneath it can be
    unaccounted for.

    The merge's recursion bound is deliberately not reproduced. A key deep
    enough that the merge stops short is still a key this template does not
    name, and the bound is the merge's to move.
    """
    missing: list[tuple[str, ...]] = []
    for key, value in shipped.items():
        path = prefix + (str(key),)
        if key not in template:
            missing.append(path)
        elif isinstance(template[key], dict) and isinstance(value, dict):
            missing.extend(_unaccounted(template[key], value, path))
    return sorted(missing)


def _divergences(relative: str) -> list[tuple[str, ...]]:
    return _unaccounted(_load(_TEMPLATE), _load(_REPO_ROOT / relative))


def _explained(path: tuple[str, ...]) -> bool:
    if path in _ACCEPTED_KEYS or path in _RENAMED_BY_MIGRATION:
        return True
    return any(path[: len(prefix)] == prefix for prefix in _ACCEPTED_SUBTREES)


def _paths(node, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Every mapping key in ``node``, as a tuple path.

    Lists are not descended into, because ``_deep_merge`` does not descend into
    one either: a list in the template makes the merge take the operator's list
    verbatim, so nothing inside one can be lost.
    """
    found: set[tuple[str, ...]] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = prefix + (str(key),)
            found.add(path)
            found |= _paths(value, path)
    return found


def _migrated(shipped: Path, tmp_path: Path) -> dict:
    """Run the real migration over a copy of ``shipped``.

    The real entry point rather than ``_deep_merge`` on purpose: the structural
    migrations that run before the merge are part of what decides where a key's
    value ends up, and they are the subject of the one test that calls this.
    """
    user_config_path = tmp_path / "config.yaml"
    shutil.copyfile(shipped, user_config_path)
    migrate_config_from_template(_TEMPLATE, user_config_path)
    return _load(user_config_path)


@pytest.mark.parametrize("relative", _SHIPPED_CONFIGS)
def test_template_covers_every_key_a_shipped_config_carries(relative: str):
    unexplained = [
        ".".join(path) for path in _divergences(relative) if not _explained(path)
    ]
    assert unexplained == [], (
        f"{relative} sets options that jiuwenswarm/resources/config.yaml does "
        f"not list, so they reach no fresh installation, never reach a config "
        f"written before they existed, and are what a merge cleaning up after "
        f"retired options removes: {unexplained}. Add each to the template, or "
        f"record beside _ACCEPTED_KEYS / _ACCEPTED_SUBTREES in this module why "
        f"it is not an option this template owes the operator."
    )


def test_every_accepted_divergence_is_still_a_divergence():
    """The list may only shrink.

    An entry that no longer describes anything is an entry that has stopped
    being reviewed, and the next reader takes the whole list on trust.
    """
    seen: set[tuple[str, ...]] = set()
    for relative in _SHIPPED_CONFIGS:
        seen.update(_divergences(relative))

    stale_keys = sorted(
        ".".join(path) for path in _ACCEPTED_KEYS if path not in seen
    )
    stale_renames = sorted(
        ".".join(path) for path in _RENAMED_BY_MIGRATION if path not in seen
    )
    stale_subtrees = sorted(
        ".".join(prefix)
        for prefix in _ACCEPTED_SUBTREES
        if not any(path[: len(prefix)] == prefix for path in seen)
    )
    assert (stale_keys, stale_renames, stale_subtrees) == ([], [], []), (
        "These entries no longer describe a key any shipped config diverges "
        "on. Remove them: "
        f"keys={stale_keys} renames={stale_renames} subtrees={stale_subtrees}"
    )


def test_the_check_would_notice_a_missing_key():
    """The mechanism this module is built on, pinned.

    Without this, a change that made ``_unaccounted`` return nothing at all
    would turn every assertion above into a tautology that passes. The same
    case pins the two rules that keep it from over-reporting: a key the
    template names is not a divergence, and a template value that is not a
    mapping ends the walk, because the merge hands the operator's value over
    whole rather than reading the template for a set of permitted sub-keys.
    """
    template = {"outer": {"kept": 1}, "open_ended": None}
    shipped = {"outer": {"kept": 2, "added": 3}, "open_ended": {"anything": 4}}

    assert _unaccounted(template, shipped) == [("outer", "added")]


@pytest.mark.parametrize("relative", _SHIPPED_CONFIGS)
def test_a_renamed_key_reaches_its_new_home(relative: str, tmp_path: Path):
    """A rename must be a rename, not a permitted disappearance."""
    shipped = _REPO_ROOT / relative
    before = _paths(_load(shipped))
    after = _paths(_migrated(shipped, tmp_path))
    for old, new in _RENAMED_BY_MIGRATION.items():
        if old not in before:
            continue
        assert new in after, (
            f"{relative} carries {'.'.join(old)}, and the migration was "
            f"supposed to carry it to {'.'.join(new)} -- which is not in the "
            f"migrated config."
        )


def test_a_renamed_key_carries_its_value_and_not_the_template_default(
    tmp_path: Path,
):
    """The shipped configs cannot show this: they set what the template does.

    Every shipped config that carries ``modes.agent.plan`` sets
    ``memory.enabled: true``, which is also the template's value, so the test
    above cannot tell a migration that ran from one that did nothing. A legacy
    config that disagrees with the template can.
    """
    template_path = tmp_path / "template.yaml"
    shutil.copyfile(_TEMPLATE, template_path)
    user_config_path = tmp_path / "config.yaml"
    user_config_path.write_text(
        "modes:\n"
        "  agent:\n"
        "    plan:\n"
        "      memory:\n"
        "        enabled: false\n",
        encoding="utf-8",
    )

    migrate_config_from_template(template_path, user_config_path)

    migrated = _load(user_config_path)
    assert migrated["modes"]["agent"]["memory"]["enabled"] is False
