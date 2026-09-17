# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Priority policy for JiuwenSwarm system prompts.

The policy is intentionally kept outside the code-mode prompt builder.  The
agent adapter opts into it for the single-agent path; code mode does not
attach this registry or its collision diagnostics.
"""

from __future__ import annotations

from enum import IntEnum, unique
from threading import RLock
from types import MappingProxyType
from typing import Mapping


@unique
class SystemPromptPriority(IntEnum):
    """Semantic priority groups with reserved gaps for future sections."""

    # Identity, safety, and task policy.
    IDENTITY = 10
    TASK_EXECUTION = 15
    SAFETY = 20

    # Tool, skill, and orchestration policy.
    TOOLS = 30
    SKILLS = 40
    SYMPHONY = 42

    # Memory and request/response contract.
    MEMORY = 50
    DAILY_MEMORY_CONTEXT = 52
    EXTERNAL_MEMORY = 55
    INPUT = 60
    A2UI = 61
    OUTPUT = 65

    # Workspace and tool-disclosure policy.
    WORKSPACE = 70
    TOOL_NAVIGATION = 72
    PROGRESSIVE_TOOL_RULES = 75
    HEARTBEAT = 78
    TASK_TOOL = 80
    SUBAGENT_TOOLS = 81
    SESSION_TOOLS = 82
    COMPLETION_SIGNAL = 83
    MODE_INSTRUCTIONS = 84

    # Runtime state and context processing.
    RUNTIME = 85
    ENV = 86
    DIRECTORY_BOUNDARIES = 87
    GOAL_PROTOCOL = 88
    OFFLOAD = 90
    GIT_STATUS = 91
    TODO = 92
    COMPRESSION_RECALL = 93
    ETERNAL_CONVERSATION = 94
    VERIFICATION_CONTRACT = 96
    VERIFICATION_REMINDER = 97
    EVOLUTION_PROTOCOL = 98
    EVOLUTION_TEAM_PROTOCOL = 99

    # Context-file and host-specific policy.
    CONTEXT = 100
    CONTEXT_AGENT = 101
    CONTEXT_SOUL = 102
    CONTEXT_IDENTITY = 103
    CONTEXT_USER = 104
    CONTEXT_HEARTBEAT = 105
    SKILL_CREATION_GUIDANCE = 106
    SKILL_CREATION_NUDGE = 107
    TEAM_SKILL_CREATION_GUIDANCE = 108
    TEAM_SKILL_CREATION_NUDGE = 109
    AVATAR_IDENTITY = 110
    GROUP_CHAT_MEMORY_NOTICE = 111
    MEMORY_FULLY_DISABLED = 112
    MEMORY_FORBIDDEN = 113
    INTERACTION_GUIDANCE = 114
    PROJECT_MEMORY = 115


_SECTION_PRIORITIES = MappingProxyType({
    # Core agent-core sections.
    "identity": SystemPromptPriority.IDENTITY,
    "safety": SystemPromptPriority.SAFETY,
    "tools": SystemPromptPriority.TOOLS,
    "skills": SystemPromptPriority.SKILLS,
    "memory": SystemPromptPriority.MEMORY,
    "daily_memory_context": SystemPromptPriority.DAILY_MEMORY_CONTEXT,
    "external_memory": SystemPromptPriority.EXTERNAL_MEMORY,
    "workspace": SystemPromptPriority.WORKSPACE,
    "tool_navigation": SystemPromptPriority.TOOL_NAVIGATION,
    "progressive_tool_rules": SystemPromptPriority.PROGRESSIVE_TOOL_RULES,
    "heartbeat": SystemPromptPriority.HEARTBEAT,
    "subagent_tools": SystemPromptPriority.SUBAGENT_TOOLS,
    "task_tool": SystemPromptPriority.TASK_TOOL,
    "session_tools": SystemPromptPriority.SESSION_TOOLS,
    "completion_signal": SystemPromptPriority.COMPLETION_SIGNAL,
    "mode_instructions": SystemPromptPriority.MODE_INSTRUCTIONS,
    "runtime": SystemPromptPriority.RUNTIME,
    "context": SystemPromptPriority.CONTEXT,
    "goal_protocol": SystemPromptPriority.GOAL_PROTOCOL,
    "verification_contract": SystemPromptPriority.VERIFICATION_CONTRACT,
    "offload": SystemPromptPriority.OFFLOAD,
    "todo": SystemPromptPriority.TODO,
    "compression_recall": SystemPromptPriority.COMPRESSION_RECALL,
    "eternal_conversation": SystemPromptPriority.ETERNAL_CONVERSATION,
    "evolution_protocol": SystemPromptPriority.EVOLUTION_PROTOCOL,
    "evolution_team_protocol": SystemPromptPriority.EVOLUTION_TEAM_PROTOCOL,
    "skill_creation_guidance": SystemPromptPriority.SKILL_CREATION_GUIDANCE,
    "skill_creation_nudge": SystemPromptPriority.SKILL_CREATION_NUDGE,
    "team_skill_creation_guidance": SystemPromptPriority.TEAM_SKILL_CREATION_GUIDANCE,
    "team_skill_creation_nudge": SystemPromptPriority.TEAM_SKILL_CREATION_NUDGE,
    "project_memory": SystemPromptPriority.PROJECT_MEMORY,

    # JiuwenSwarm single-agent sections.
    "task_execution": SystemPromptPriority.TASK_EXECUTION,
    "input": SystemPromptPriority.INPUT,
    "a2ui": SystemPromptPriority.A2UI,
    "output": SystemPromptPriority.OUTPUT,
    "env": SystemPromptPriority.ENV,
    "directory_boundaries": SystemPromptPriority.DIRECTORY_BOUNDARIES,
    "git_status": SystemPromptPriority.GIT_STATUS,
    "symphony_orchestration": SystemPromptPriority.SYMPHONY,
    "context.agent": SystemPromptPriority.CONTEXT_AGENT,
    "context.soul": SystemPromptPriority.CONTEXT_SOUL,
    "context.identity": SystemPromptPriority.CONTEXT_IDENTITY,
    "context.user": SystemPromptPriority.CONTEXT_USER,
    "context.heartbeat": SystemPromptPriority.CONTEXT_HEARTBEAT,
    "avatar_identity": SystemPromptPriority.AVATAR_IDENTITY,
    "group_chat_memory_notice": SystemPromptPriority.GROUP_CHAT_MEMORY_NOTICE,
    "memory_fully_disabled": SystemPromptPriority.MEMORY_FULLY_DISABLED,
    "memory_forbidden": SystemPromptPriority.MEMORY_FORBIDDEN,
    "interaction_guidance": SystemPromptPriority.INTERACTION_GUIDANCE,
})


class PromptPriorityRegistry:
    """Resolve static and process-wide runtime prompt-section priorities.

    Static entries are the checked-in priority policy.  Entries added through
    :meth:`register_section` live for the lifetime of this process and are
    removed through :meth:`unregister_section`.
    """

    def __init__(self, priorities: Mapping[str, int]) -> None:
        self._lock = RLock()
        self._static_priorities = {
            str(name): int(priority)
            for name, priority in priorities.items()
        }
        self._runtime_priorities: dict[str, int] = {}

    def priority_for(self, section_name: str, fallback_priority: int) -> int:
        """Return the registered priority or preserve the caller fallback."""
        with self._lock:
            runtime_priority = self._runtime_priorities.get(str(section_name))
            if runtime_priority is not None:
                return runtime_priority
            return self._static_priorities.get(str(section_name), int(fallback_priority))

    def names_for_priority(self, priority: int) -> tuple[str, ...]:
        """Return registered section names that reserve ``priority``."""
        target_priority = int(priority)
        with self._lock:
            names = [
                name
                for name, section_priority in self._combined_priorities().items()
                if section_priority == target_priority
            ]
        return tuple(sorted(names))

    def register_section(self, section_name: str, priority: int) -> None:
        """Register a runtime section priority.

        Static entries are authoritative and must be registered with their
        declared priority.  Re-registering an unknown name with a different
        priority is also rejected so one stable section name cannot silently
        move between ordering groups.
        """

        normalized_name = str(section_name)
        if not normalized_name:
            raise ValueError("Prompt section name must not be empty")
        normalized_priority = int(priority)

        with self._lock:
            static_priority = self._static_priorities.get(normalized_name)
            if static_priority is not None:
                if static_priority != normalized_priority:
                    raise ValueError(
                        "Prompt section %r is defined with priority %s; "
                        "cannot register it with priority %s"
                        % (normalized_name, static_priority, normalized_priority)
                    )
                return

            previous_priority = self._runtime_priorities.get(normalized_name)
            if previous_priority is not None and previous_priority != normalized_priority:
                raise ValueError(
                    "Prompt section %r is already registered with priority %s; "
                    "cannot register it with priority %s"
                    % (normalized_name, previous_priority, normalized_priority)
                )
            self._runtime_priorities[normalized_name] = normalized_priority

    def unregister_section(self, section_name: str) -> None:
        """Remove a runtime section priority if one was registered.

        Static policy entries intentionally remain available after a section
        is removed from a builder; only automatically registered runtime
        entries are unbound.
        """

        normalized_name = str(section_name)
        with self._lock:
            if normalized_name not in self._static_priorities:
                self._runtime_priorities.pop(normalized_name, None)

    def priorities(self) -> Mapping[str, int]:
        """Return a read-only snapshot of static and runtime priorities."""
        with self._lock:
            return MappingProxyType(self._combined_priorities())

    def _combined_priorities(self) -> dict[str, int]:
        combined = dict(self._static_priorities)
        combined.update(self._runtime_priorities)
        return combined


SYSTEM_PROMPT_PRIORITY_REGISTRY = PromptPriorityRegistry(_SECTION_PRIORITIES)


__all__ = [
    "PromptPriorityRegistry",
    "SYSTEM_PROMPT_PRIORITY_REGISTRY",
    "SystemPromptPriority",
]
