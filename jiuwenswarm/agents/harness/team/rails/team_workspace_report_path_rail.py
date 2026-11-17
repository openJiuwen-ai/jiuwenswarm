# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt rail for reporting real team workspace artifact paths."""

from __future__ import annotations

from pathlib import Path

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail


class TeamWorkspaceReportPathRail(DeepAgentRail):
    """Inject guidance for reporting real shared-workspace artifact paths."""

    priority = 5

    def __init__(
        self,
        *,
        root_dir: str,
        team_id: str | None = None,
        language: str = "cn",
    ) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._root_dir = str(Path(root_dir))
        self._team_id = team_id or ""
        self._language = language

    def init(self, agent) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("team_workspace_report_paths")
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        _ = ctx
        if self.system_prompt_builder is None:
            return

        mount = ".team/"
        # Dual-root: collaboration is team-ws / `.team/`; user open/preview is session
        # `project_dir` (当前项目目录 in RuntimePromptRail). Do not invent delivery paths
        # under team-ws; one send per deliverable; report the same project-dir path.
        content = (
            "# Team Workspace Artifact Paths\n\n"
            "- Session project directory (`project_dir` / 当前项目目录，见运行时目录上下文): "
            "user-facing open/preview root for deliverables.\n"
            f"- Team collaboration root (`.team/` mount target when linked): `{self._root_dir}`\n"
            f"- Internal mount path: `{mount}` - use only for teammate read/write collaboration.\n"
            "- Collaboration drafts may use `.team/...`.\n"
            "- User-facing final deliverables must exist under the session project directory "
            "(write there directly, or copy once into it before delivery). "
            "Do not invent an absolute path under the team collaboration root for user delivery.\n"
            "- If the runtime directory context shows the session project directory as unset "
            "(当前项目目录：未设置) or that section is missing, use the current working directory "
            "(cwd) / task work path from that context as the user-facing delivery root instead; "
            "still do not invent a path under the team collaboration root.\n"
            "- Call `send_file_to_user` once per user-facing deliverable with the real absolute "
            "path of the file on disk (prefer the path returned by `write_file`, the generator, "
            "or realpath). That path must be under the session project directory when set, "
            "otherwise under the cwd / task work path above. "
            "Do not send the same deliverable twice under different aliases "
            "(e.g. both a `.team/...` path and a team-workspace absolute path).\n"
            "- A file name or path in `send_message`, the final response, or a task-completion "
            "summary does not deliver the file and must not replace `send_file_to_user`.\n"
            "- Report to teammates the real absolute path under the session project directory "
            "(same path used for the user-facing deliverable); do not rewrite it under the "
            "collaboration root or as a bare `.team/...` string.\n"
            "- The mount is already the shared workspace root; do not add a team id or "
            "`team-workspace` segment after `.team/`.\n"
        )
        self.system_prompt_builder.add_section(
            PromptSection(
                name="team_workspace_report_paths",
                content={"cn": content, "en": content},
                priority=67,
            )
        )
