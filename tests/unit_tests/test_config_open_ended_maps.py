# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An open-ended option must be shipped bare, not as an empty mapping.

``_deep_merge`` walks the *template's* keys, so a mapping in the template is
the set of sub-keys the merge accounts for under that option. Writing an
open-ended option as ``{}`` says "no sub-key belongs here" about precisely the
options whose contents only the operator can supply:
``permissions.owner_scopes``, keyed by channel ID; the three OTLP ``headers``
maps, keyed by header name; the distributed leader's ``custom_headers``. A
template value that is not a mapping carries no such claim -- the merge hands
the operator's own value over whatever shape it has.

``permissions.owner_scopes`` made the consequence concrete. The Web settings
page writes it, and the merge -- projecting the operator's file through a
template that carried ``{}`` -- took the authorisation back out, which reads
as the page not saving.

The rule is about open-ended options: the ones whose sub-keys only the
operator can name. An option whose sub-keys the code itself names is a
different case, and one is exempted below with its reason. ``{}`` there still
costs the operator the value on upgrade, but through the merge's disposal of
keys it has no default for, which is not this module's subject.

This module asserts a property of the three shipped templates, and asserts it
against the templates themselves rather than by driving the merge over them.
That is deliberate, for two reasons.

The rule is about what a template is allowed to claim. Re-deriving it from
what the merge currently does with a key it has no default for would tie a
statement about the templates to a disposal policy that lives elsewhere, has
changed before, and is not this file's subject. The templates own this rule
whatever the merge decides.

It also removes the depth question. The merge stops descending at a fixed
recursion bound, so an empty mapping below that bound is harmless by luck
rather than by design; pinning the property to the bound would pin a proxy the
merge is free to move. The templates are held to the rule at every depth,
which is the rule as stated.

What is still exercised end to end is the half that must hold for the rule to
be worth anything: an operator's value under a bare key survives the real
migration. That is true of the merge as it stands and of any merge that keeps
faith with the template, so it belongs in code rather than in prose.

Both paths are always given explicitly --
``migrate_config_from_template(template_path, user_config_path)`` -- and
``user_config_path`` is always inside pytest's ``tmp_path``. No test in this
module may call the no-argument ``ensure_config_migrated_from_template()``
instead. That wrapper resolves the running installation's workspace and
rewrites the config file it finds there, so a later edit reaching for the
convenient call would destroy the developer's own configuration -- which is
the defect this module exists to describe.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.config import migrate_config_from_template

_RESOURCES = Path(__file__).resolve().parents[2] / "jiuwenswarm" / "resources"

_SHIPPED_TEMPLATES = (
    "config.yaml",
    "config.team.distributed.leader.yaml",
    "config.team.distributed.teammate.yaml",
)

# Mappings a template ships empty for a reason that is not an open-ended
# option. Keyed by the last path element, because the same option repeats once
# per model entry. Asserted in both directions: an unexplained empty mapping
# fails, and so does an entry that no longer matches anything, so the list
# cannot quietly become the specification.
_ACCEPTED_EMPTY_MAPPINGS: dict[str, str] = {
    "model_config_obj": (
        "Not open-ended: its sub-keys are named by the code -- temperature, "
        "top_p, context_window, reasoning_level -- so the template writing it "
        "as a mapping claims nothing the operator can disprove. The defaults "
        "it used to carry were removed deliberately, leaving {}. An operator's "
        "value under it is still lost on upgrade, but through the merge's "
        "disposal of keys the template has no default for, which is the "
        "merge's rule rather than the template's, and several readers require "
        "a mapping here and reject a bare key, so it cannot be written bare "
        "without changing them first."
    ),
}


def _mappings_reachable_through_mappings(
    node, prefix: tuple[str, ...] = ()
) -> dict[tuple[str, ...], dict]:
    """Every mapping the merge can walk into, keyed by its path.

    Only mappings reached through other mappings are returned. ``_deep_merge``
    never descends into a list, nor past a value that is not a mapping, so a
    mapping in either position is handed over whole and cannot be read as a
    set of permitted sub-keys.
    """
    found: dict[tuple[str, ...], dict] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            path = prefix + (str(key),)
            if isinstance(value, dict):
                found[path] = value
                found.update(_mappings_reachable_through_mappings(value, path))
    return found


def _template(name: str) -> dict:
    return yaml.safe_load((_RESOURCES / name).read_text(encoding="utf-8"))


def _migrated(name: str, user: dict, tmp_path: Path) -> dict:
    """Run the real migration over ``user``, against a copy of a shipped template.

    The shipped file is copied rather than re-serialised, so what the operator's
    config is projected through is the template as it ships, comments included.
    """
    template_path = tmp_path / "template.yaml"
    shutil.copyfile(_RESOURCES / name, template_path)
    user_config_path = tmp_path / "config.yaml"
    user_config_path.write_text(
        yaml.safe_dump(user, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    migrate_config_from_template(template_path, user_config_path)
    return yaml.safe_load(user_config_path.read_text(encoding="utf-8")) or {}


class TestTemplatesShipOpenEndedMapsBare:
    """No shipped template may write an open-ended option as an empty mapping."""

    TEMPLATES = _SHIPPED_TEMPLATES

    @pytest.mark.parametrize("name", TEMPLATES)
    def test_no_option_is_shipped_as_an_empty_mapping(self, name: str):
        """Stated at every depth, not at the depth the merge happens to reach.

        An empty mapping the merge stops short of is harmless today and is
        harmless only for as long as the recursion bound stays where it is.
        The bound is the merge's to move; the templates are held to the rule
        wherever the mapping sits.
        """
        empty = sorted(
            ".".join(path)
            for path, node in _mappings_reachable_through_mappings(
                _template(name)
            ).items()
            if not node
        )
        offenders = [
            path
            for path in empty
            if path.rsplit(".", 1)[-1] not in _ACCEPTED_EMPTY_MAPPINGS
        ]
        assert offenders == [], (
            f"{name} writes these open-ended options as an empty mapping, which "
            f"tells the merge no sub-key belongs under them: {offenders}. Write "
            "each key bare instead, so the merge hands the operator's value "
            "over untouched, or record beside _ACCEPTED_EMPTY_MAPPINGS in this "
            "module why the option is not open-ended."
        )

    def test_every_accepted_empty_mapping_still_describes_one(self):
        """An exemption that matches nothing has stopped explaining anything."""
        seen: set[str] = set()
        for name in self.TEMPLATES:
            for path, node in _mappings_reachable_through_mappings(
                _template(name)
            ).items():
                if not node:
                    seen.add(path[-1])
        stale = sorted(set(_ACCEPTED_EMPTY_MAPPINGS) - seen)
        assert stale == [], (
            f"These entries in _ACCEPTED_EMPTY_MAPPINGS match no empty mapping "
            f"in any shipped template: {stale}. Delete each one; a reason that "
            "explains nothing is a reason nobody will check."
        )

    @pytest.mark.parametrize("name", TEMPLATES)
    def test_owner_scopes_is_bare_and_keeps_what_the_settings_page_wrote(
        self, name: str, tmp_path: Path
    ):
        template = _template(name)
        assert template["permissions"]["owner_scopes"] is None, name

        saved_by_the_settings_page = {
            "slack": {"U0000000001": {"tools": {"bash": "ls *"}}}
        }
        user = copy.deepcopy(template)
        user["permissions"]["owner_scopes"] = saved_by_the_settings_page

        merged = _migrated(name, user, tmp_path)
        assert (
            merged["permissions"]["owner_scopes"] == saved_by_the_settings_page
        ), name

    @pytest.mark.parametrize("name", TEMPLATES)
    def test_otlp_headers_are_bare_and_keep_an_operator_header(
        self, name: str, tmp_path: Path
    ):
        template = _template(name)
        telemetry = template["telemetry"]
        assert telemetry["headers"] is None, name
        assert telemetry["traces"]["headers"] is None, name
        assert telemetry["metrics"]["headers"] is None, name

        user = copy.deepcopy(template)
        user["telemetry"]["headers"] = {"authorization": "Bearer x"}
        user["telemetry"]["traces"]["headers"] = {"x-scope": "traces"}

        merged = _migrated(name, user, tmp_path)
        assert merged["telemetry"]["headers"] == {"authorization": "Bearer x"}, name
        assert merged["telemetry"]["traces"]["headers"] == {"x-scope": "traces"}, name
