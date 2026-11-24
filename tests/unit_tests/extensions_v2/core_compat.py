"""Test gates for extension APIs that land in agent-core before Swarm updates its pin."""

from inspect import signature

import pytest

from openjiuwen.core.extension import Registry


requires_rooted_startup = pytest.mark.skipif(
    "roots" not in signature(Registry.load_installed).parameters,
    reason="requires agent-core Registry.load_installed(roots)",
)
