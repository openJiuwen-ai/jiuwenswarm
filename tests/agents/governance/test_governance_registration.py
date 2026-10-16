# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registration and opt-in mount tests for the governance rails.

Asserts the rail is declared in the manifest catalog, registers into
openjiuwen's rail-provider registry, builds through its factory, and is mounted
by ``config_specs`` only when the team config enables it.
"""

from __future__ import annotations

import unittest

from openjiuwen.agent_teams.harness.manifest import (
    get_catalog,
    resolve_factory,
)
from openjiuwen.harness.schema import deep_agent_spec as das

from jiuwenswarm.agents.swarm import register_swarm_providers, registry
from jiuwenswarm.agents.swarm.config_specs import _governance_rails
from jiuwenswarm.agents.harness.common.rails.usage_report_rail import UsageReportRail


class TestRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_swarm_providers()

    def test_constant_exported(self) -> None:
        self.assertEqual(registry.USAGE_REPORT, "swarm.usage_report")

    def test_element_in_catalog(self) -> None:
        self.assertIn("swarm.usage_report", get_catalog())

    def test_element_registered_as_rail(self) -> None:
        self.assertIn("swarm.usage_report", das._RAIL_PROVIDER_REGISTRY)

    def test_registry_catalog_parity(self) -> None:
        # The swarm.* constant must have a catalog descriptor (parity invariant
        # the upstream manifest test enforces globally).
        self.assertIn(registry.USAGE_REPORT, get_catalog())

    def test_factory_builds_rail(self) -> None:
        descriptor = get_catalog()["swarm.usage_report"]
        factory = resolve_factory(descriptor.factory_ref)
        rail = factory({"report_path": "/tmp/r.json", "default_label": "x"}, None)
        self.assertIsInstance(rail, UsageReportRail)


class TestOptInMount(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        self.assertEqual(_governance_rails({}), [])
        self.assertEqual(_governance_rails({"usage_report": {}}), [])
        self.assertEqual(_governance_rails({"usage_report": {"enabled": False}}), [])

    def test_enabled_mounts_one_spec(self) -> None:
        specs = _governance_rails({"usage_report": {"enabled": True, "report_path": "/tmp/u.json"}})
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].type, registry.USAGE_REPORT)
        self.assertEqual(specs[0].params["report_path"], "/tmp/u.json")

    def test_only_declared_keys_are_forwarded(self) -> None:
        specs = _governance_rails({
            "usage_report": {"enabled": True, "default_label": "stage", "unknown_key": 1},
        })
        self.assertEqual(specs[0].params, {"default_label": "stage"})


if __name__ == "__main__":
    unittest.main()
