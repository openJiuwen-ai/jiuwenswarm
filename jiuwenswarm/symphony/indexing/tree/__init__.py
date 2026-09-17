from __future__ import annotations

__all__ = [
    "DynamicTreeConfig",
    "Skill",
    "SkillStatus",
    "TreeBuildConfig",
    "TreeBuilder",
    "TreeManagerConfig",
    "TreeNode",
    "build_tree",
]


def __getattr__(name: str):
    if name in {"DynamicTreeConfig", "Skill", "SkillStatus", "TreeNode", "TreeBuildConfig", "TreeManagerConfig"}:
        from .schema import DynamicTreeConfig, Skill, SkillStatus, TreeBuildConfig, TreeManagerConfig, TreeNode

        exports = {
            "DynamicTreeConfig": DynamicTreeConfig,
            "Skill": Skill,
            "SkillStatus": SkillStatus,
            "TreeBuildConfig": TreeBuildConfig,
            "TreeManagerConfig": TreeManagerConfig,
            "TreeNode": TreeNode,
        }
        value = exports.get(name)
        if value is not None:
            return value
    if name in {"TreeBuilder", "build_tree"}:
        from .builder import TreeBuilder, build_tree

        exports = {"TreeBuilder": TreeBuilder, "build_tree": build_tree}
        value = exports.get(name)
        if value is not None:
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
