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
        project_dir: str | None = None,
    ) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._root_dir = str(Path(root_dir))
        self._team_id = team_id or ""
        self._language = language
        self._project_dir = str(Path(project_dir)) if project_dir else None

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

        mount = f".team/{self._team_id}/" if self._team_id else ".team/"
        sample = str(Path(self._root_dir) / "prd-review-memo.md")
        content = (
            "# Team Workspace Artifact Paths\n\n"
            f"- Team workspace absolute root: `{self._root_dir}`\n"
            f"- Internal mount path: `{mount}`\n"
            "- Use the internal mount path only for tool read/write operations.\n"
            "- For every final deliverable file, call `send_file_to_user` with its real absolute filesystem path "
            "before marking the task complete or sending the completion message.\n"
            "- A file name or path in `send_message`, the final response, or a task-completion summary does not "
            "deliver the file and must not replace `send_file_to_user`.\n"
            "- After `send_file_to_user` succeeds, report the real absolute filesystem path under the team "
            "workspace absolute root, not the `.team/...` mount path.\n"
            "- If a generated artifact path contains `.team/<team>/team-workspace/`, remove that mount prefix and "
            "join the remaining file name under the team workspace absolute root before sending and reporting it.\n"
            f"- Example absolute path to pass to `send_file_to_user`: `{sample}`\n"
        )
        if self._project_dir:
            deliverable_sample = str(Path(self._project_dir) / "prd-review-memo.md")
            content += (
                "\n## Deliverables in the Task Working Directory\n\n"
                "- User-facing deliverables live in the current working directory (the task workspace): "
                f"`{self._project_dir}` — not under the team workspace root.\n"
                f"- Example deliverable path to pass to `send_file_to_user`: `{deliverable_sample}`\n"
                "- After `send_file_to_user` succeeds for such a deliverable, report its real absolute path "
                "under the task working directory. The mount-prefix rewrite rules above only apply to files "
                "that are actually stored in the team shared workspace.\n"
            )
        self.system_prompt_builder.add_section(
            PromptSection(
                name="team_workspace_report_paths",
                content={"cn": content, "en": content},
                priority=67,
            )
        )
