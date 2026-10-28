# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuWenSwarm Rails for DeepAgent integration.

注意：工具权限护栏已切换为 openjiuwen 实现；此处保留同名导出以维持兼容。
"""

from importlib import import_module

# Importing a permission submodule must not prepare every unrelated rail or
# Team toolkit. Preserve the public classes and resolve their real modules on
# explicit use; mutable rail instances remain owned by individual sessions.
_EXPORTS = {
    "PermissionInterruptRail": (
        "openjiuwen.harness.rails.security",
        "PermissionInterruptRail",
    ),
    "AvatarPromptRail": (".avatar_rail", "AvatarPromptRail"),
    "BrowserTaskPromptRail": (".browser_task_prompt_rail", "BrowserTaskPromptRail"),
    "ProjectMemoryRail": (".project_memory_rail", "ProjectMemoryRail"),
    "ResponsePromptRail": (".response_prompt_rail", "ResponsePromptRail"),
    "RuntimePromptRail": (".runtime_prompt_rail", "RuntimePromptRail"),
    "SymphonyOrchestrationRail": (".symphony", "SymphonyOrchestrationRail"),
    "MemberSkillToolkitRail": (
        "jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail",
        "MemberSkillToolkitRail",
    ),
    "StructuredAskUserRail": (".ask_user_rail", "StructuredAskUserRail"),
    "MultimodalImageRail": (".multimodal_image_rail", "MultimodalImageRail"),
    "JiuSwarmStreamEventRail": (".stream_event_rail", "JiuSwarmStreamEventRail"),
}

__all__ = [
    "JiuSwarmStreamEventRail",
    "MultimodalImageRail",
    "PermissionInterruptRail",
    "AvatarPromptRail",
    "BrowserTaskPromptRail",
    "ProjectMemoryRail",
    "ResponsePromptRail",
    "RuntimePromptRail",
    "SymphonyOrchestrationRail",
    "MemberSkillToolkitRail",
    "StructuredAskUserRail",
]


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, symbol = _EXPORTS[name]
    value = getattr(import_module(module, __name__), symbol)
    globals()[name] = value
    return value
