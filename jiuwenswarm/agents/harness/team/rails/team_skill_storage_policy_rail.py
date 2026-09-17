# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inject team skill storage policy into member prompts.

Backward-incompatible rename: the constructor keyword ``team_skills_dir`` is now
``team_skill_visibility_file``. Teams own no ``skills/`` directory any more,
only a ``skills-visibility.json`` document at their workspace root.
"""

from __future__ import annotations

from openjiuwen.agent_teams.paths import SKILL_VISIBILITY_FILENAME
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail


class TeamSkillStoragePolicyRail(DeepAgentRail):
    """Tell team members where skill authoring outputs must be stored.

    The policy text is identical for every member of a team and stays in the
    system prompt, so the whole team shares one cacheable prompt prefix. It
    names only team-level paths; the member's own workspace is per-member and
    is delivered by the team rail as part of the member's identity, together
    with the rule that new skills must not be created there.
    """

    priority = 5
    SECTION_NAME = "team_skill_storage_policy"
    SECTION_PRIORITY = 39

    def __init__(
        self,
        *,
        global_skills_dir: str,
        team_workspace_root: str | None = None,
        team_skill_visibility_file: str | None = None,
    ) -> None:
        """Bind the rail to the team-level paths named by the policy.

        Args:
            global_skills_dir: The one physical Skill library every agent reads.
            team_workspace_root: Team shared workspace root, which holds no
                Skill sources.
            team_skill_visibility_file: Team ``skills-visibility.json`` path,
                which declares visibility only and is never a Skill source.
        """
        super().__init__()
        self.system_prompt_builder = None
        self._global_skills_dir = global_skills_dir
        self._team_workspace_root = team_workspace_root
        self._team_skill_visibility_file = team_skill_visibility_file

    def init(self, agent) -> None:
        """Capture the prompt builder owned by the member."""
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        """Remove the injected policy section."""
        _ = agent
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Inject the storage policy before each model call."""
        _ = ctx
        if self.system_prompt_builder is None:
            return

        self.system_prompt_builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={
                    "cn": self._build_cn_content(),
                    "en": self._build_en_content(),
                },
                priority=self.SECTION_PRIORITY,
            )
        )

    def _build_cn_content(self) -> str:
        """Build the Chinese prompt section."""
        visibility_only_paths = self._format_visibility_only_paths("cn")
        return (
            "# Team Skill 存储规则\n\n"
            "在 team 模式中，凡是创建、转换、修改或优化 Skill / Swarm Skill / Team Skill，"
            "最终 skill 源目录必须写入全局公共技能目录。\n\n"
            f"- 全局公共技能目录：`{self._global_skills_dir}`\n"
            f"- 新 skill 的入口文件必须是：`{self._global_skills_dir}/<skill-name>/SKILL.md`\n"
            "- `assets/`、`scripts/`、`references/`、模板、示例、验证脚本等配套资源，"
            "必须放在同一个 `<skill-name>/` 目录下。\n"
            f"- 成员 workspace 和 team workspace 下只有 `{SKILL_VISIBILITY_FILENAME}` "
            "这一个可见性声明文件，绝不能在其中创建 skill 实体；它们不存放任何 skill 源文件。\n"
            f"{visibility_only_paths}"
            "- 所有 skill 实体唯一存放于上面的全局公共技能目录，也不要写到当前目录或临时目录。"
        )

    def _build_en_content(self) -> str:
        """Build the English prompt section."""
        visibility_only_paths = self._format_visibility_only_paths("en")
        return (
            "# Team Skill Storage Policy\n\n"
            "In team mode, whenever you create, convert, modify, or optimize a Skill, "
            "Swarm Skill, or Team Skill, the final skill source directory must be "
            "written under the global shared skills directory.\n\n"
            f"- Global shared skills directory: `{self._global_skills_dir}`\n"
            f"- A new skill entry file must be: `{self._global_skills_dir}/<skill-name>/SKILL.md`\n"
            "- Put related `assets/`, `scripts/`, `references/`, templates, examples, "
            "and validation scripts under the same `<skill-name>/` directory.\n"
            f"- A member workspace and the team workspace hold a single "
            f"`{SKILL_VISIBILITY_FILENAME}` visibility declaration and nothing else "
            "about skills; never create a skill entity inside them.\n"
            f"{visibility_only_paths}"
            "- Every skill entity lives in the global shared skills directory above, "
            "and never in the current directory or a temporary directory."
        )

    def _format_visibility_only_paths(self, language: str) -> str:
        """Format the team-level paths that declare visibility but store nothing.

        Only team-level paths belong here: they are identical for every member,
        so they keep the prompt prefix shared. The member's own workspace is
        per-member and is told to it by the team rail as part of its identity.
        """
        separator = "：" if language == "cn" else ": "
        lines: list[str] = []
        if self._team_workspace_root:
            label = "team 共享工作区" if language == "cn" else "Team shared workspace"
            lines.append(f"- {label}{separator}`{self._team_workspace_root}`\n")
        if self._team_skill_visibility_file:
            label = (
                "team skill 可见性声明文件"
                if language == "cn"
                else "Team skill visibility declaration file"
            )
            lines.append(f"- {label}{separator}`{self._team_skill_visibility_file}`\n")
        return "".join(lines)

__all__ = ["TeamSkillStoragePolicyRail"]
