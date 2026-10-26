"""Replaceable installed-skill retrieval, independent of the agent loop.

Importing this package does not load models or start background workers.
"""

from .types import Candidate, SkillDocument

__all__ = ["Candidate", "SkillDocument"]


def build_rail(*, config_provider, skill_rail_provider):
    """The only host integration API; disabled rails do not create an index."""
    from .rail import SkillSelectionRail

    return SkillSelectionRail(
        config_provider=config_provider, skill_rail_provider=skill_rail_provider
    )
