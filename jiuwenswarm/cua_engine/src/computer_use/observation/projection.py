"""Full display projection; rules and ordering live in projection_rules.

These compatibility entry points return the same native-source/provenance
contract used by rendering and observation coverage. They never route input.
"""
from .projection_rules.pipeline import project as projectTree
from .projection_rules.policy import secondary_actions as secondary_actions


def prepare_tree(root, context=None):
    return projectTree(root, context)["roots"]
